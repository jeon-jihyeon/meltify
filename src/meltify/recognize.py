"""OCR and speech for the pixels and sound `read` finds, with results cited in place

One worker reads every job in turn, since Paddle and MLX hold large models in memory.
Identical content is read once, and every engine result is cached by content hash under
${XDG_CACHE_HOME:-~/.cache}/meltify, so a rerun only pays for what changed
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tempfile
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from meltify.converters import Block, RecognizeJob
from meltify.engines.asr import Segment
from meltify.engines.consensus import EngineReading, compare
from meltify.engines.ocr import TextBox
from meltify.evidence import Src, _clock, _num, finding
from meltify.safe import MissingTool, attempt

# Body text comes from the first engine here that read anything, then the rest in order
BODY_ORDER = ("vision", "paddle")
# Raised when no engine was found at all, which the run already warned about once
NO_ENGINE = ("ocr engine", "speech engine")
CACHE_VERSION = 1

Box = tuple[float, float, float, float]


def cache_root() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "meltify"


@dataclass(frozen=True)
class Options:
    engines: str = "auto"
    asr: str = "auto"
    lang: str = "ko"
    budget: float = 600.0
    frames: int = 20
    refresh: bool = False
    dpi: int = 300
    sharpen: bool = True
    tile_max: int = 2576
    scene: float = 0.3
    dedup_distance: int = 4


@dataclass
class Outcome:
    blocks: list[Block] = field(default_factory=list)
    disputed: list[dict[str, Any]] = field(default_factory=list)
    # Why the job is still pending: "budget", "engine" or "failed"
    left: str | None = None


@dataclass(frozen=True)
class Picture:
    size: tuple[int, int]  # source px, the space every box is stored in
    readings: list[EngineReading]


@dataclass(frozen=True)
class Frame:
    t: float
    sha: str
    picture: Picture | None


@dataclass(frozen=True)
class Footage:
    speech: list[Segment] | None  # None when no speech engine could listen
    scenes: list[float]
    frames: list[Frame]


class Cache:
    def __init__(self, root: Path, refresh: bool) -> None:
        self.root = root
        self.refresh = refresh

    def path(self, kind: str, sha: str, engine: str, lang: str, prep: str) -> Path:
        name = re.sub(r"[^\w.\-]+", "_", f"{sha}-{engine}-{lang}-{prep}")
        return self.root / kind / sha[:2] / f"{name}.json"

    def get(self, *key: str) -> dict[str, Any] | None:
        if self.refresh:
            return None
        try:
            value = json.loads(self.path(*key).read_text("utf-8"))
        except (OSError, ValueError):
            return None
        return value if value.get("v") == CACHE_VERSION else None

    def put(self, value: dict[str, Any], *key: str) -> None:
        target = self.path(*key)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps({"v": CACHE_VERSION, **value}, ensure_ascii=False), "utf-8")
            os.replace(tmp, target)
        except OSError:
            # A read-only cache only costs speed on the next run
            pass


def _box(b: Box | None, k: float) -> Box | None:
    return None if b is None else (b[0] * k, b[1] * k, b[2] * k, b[3] * k)


def _where(src: Src) -> str:
    # The box part of a cite, so each line reads as a suffix of its block's cite
    if src.bbox is None:
        return ""
    return f"@{src.unit}(" + ",".join(_num(v) for v in src.bbox) + ")"


def _span(start: float, end: float) -> str:
    return f"@{_clock(start)}-{_clock(end)}"


def _placer(src: Src, unit: str, k: Box) -> Callable[[Box | None], Src]:
    """Maps a box in source px to the job's own space: x0 + x * sx, y0 + y * sy"""
    x0, y0, sx, sy = k

    def place(box: Box | None) -> Src:
        if box is None:
            return src
        mapped = (x0 + box[0] * sx, y0 + box[1] * sy, x0 + box[2] * sx, y0 + box[3] * sy)
        return replace(src, bbox=tuple(round(v, 1) for v in mapped), unit=unit)

    return place


class Recognizer:
    """Reads jobs once per unique content, with one engine instance per name for the run"""

    def __init__(
        self, opts: Options, settings: dict[str, Any], warn: Callable[[str], None]
    ) -> None:
        self.opts = opts
        self.settings = settings
        self.cache = Cache(cache_root(), opts.refresh)
        self.warned: set[str] = set()
        self._warn = warn
        self._ocr: list[Any] | None = None
        self._asr: Any = None
        self._asr_ready = False
        self._file_sha: dict[Path, str] = {}
        self.tmp = Path(tempfile.gettempdir())
        # Unique contents this run, and how many came straight from the cache
        self.seen = 0
        self.cached = 0

    def warn(self, message: str) -> None:
        if message not in self.warned:
            self.warned.add(message)
            self._warn(message)

    # Engines

    def ocr_engines(self) -> list[Any]:
        if self._ocr is None:
            from meltify.engines import ocr as engines

            chosen = engines.select(self.opts.engines, self.settings)
            for e in chosen:
                if (why := e.missing()) is not None:
                    raise MissingTool(e.name, why)
            rank = {n: i for i, n in enumerate(BODY_ORDER)}
            self._ocr = sorted(chosen, key=lambda e: rank.get(e.name, len(rank)))
            if not chosen:
                self.warn(
                    "no local OCR engine, so images stay listed as needs. pip install ocrmac"
                    " on macOS, meltify doctor --install ocr-paddle elsewhere, or --engines NAME"
                )
        return self._ocr

    def asr_engine(self) -> Any:
        if not self._asr_ready:
            from meltify.engines import asr

            self._asr_ready = True
            try:
                self._asr = asr.select(self.opts.asr, self.settings)
            except MissingTool as e:
                if self.opts.asr != "auto":
                    raise
                self.warn(f"no speech engine, so audio stays listed as needs: {e.hint}")
        return self._asr

    # Content keys

    def file_sha(self, path: Path) -> str:
        from meltify.files import sha256

        if path not in self._file_sha:
            self._file_sha[path] = sha256(path)
        return self._file_sha[path]

    def key(self, job: RecognizeJob) -> str:
        if job.data is not None:
            return hashlib.sha256(job.data).hexdigest()
        assert job.path is not None
        sha = self.file_sha(job.path)
        if job.kind == "page":
            return hashlib.sha256(f"{sha}#p{job.src.page}".encode()).hexdigest()
        return sha

    # The run

    def run(self, jobs: list[RecognizeJob], transcribed: Collection[Src] = ()) -> list[Outcome]:
        """Outcomes in job order. Cached content comes first, then the rest within budget"""
        outcomes = [Outcome() for _ in jobs]
        if not jobs:
            return outcomes
        if any(
            j.kind in ("image", "page") or (j.kind == "video" and self.opts.frames > 0)
            for j in jobs
        ):
            self.ocr_engines()
        if any(j.kind in ("audio", "video") for j in jobs):
            self.asr_engine()

        groups: dict[tuple[str, str, bool], list[int]] = {}
        for i, job in enumerate(jobs):
            listen = job.kind == "audio" or (job.kind == "video" and job.src not in transcribed)
            kind = "picture" if job.kind in ("image", "page") else job.kind
            groups.setdefault((kind, self.key(job), listen), []).append(i)

        with tempfile.TemporaryDirectory(prefix="meltify-read-") as tmp:
            self.tmp = Path(tmp)
            pending = []
            for group, members in groups.items():
                if not self._settle(group, members, jobs, outcomes, compute=False):
                    pending.append((group, members))
            self.seen += len(groups)
            self.cached += len(groups) - len(pending)
            start = time.monotonic()
            for group, members in pending:
                if time.monotonic() - start >= self.opts.budget:
                    for i in members:
                        outcomes[i].left = "budget"
                    continue
                self._settle(group, members, jobs, outcomes, compute=True)
        return outcomes

    def _settle(
        self,
        group: tuple[str, str, bool],
        members: list[int],
        jobs: list[RecognizeJob],
        outcomes: list[Outcome],
        compute: bool,
    ) -> bool:
        kind, sha, listen = group
        first = jobs[members[0]]
        where = first.src.cite()
        try:
            if kind == "picture":
                got: Any = self.picture(sha, lambda: self._load(first), where, first.kind, compute)
            elif kind == "audio":
                got = self.speech(sha, first.path, where, compute)
            else:
                got = self.footage(sha, first.path, where, listen, compute)
        except MissingTool as e:
            if e.name not in NO_ENGINE:
                self.warn(f"{e.name} not available for {where}: {e.hint}")
            for i in members:
                outcomes[i].left = "engine"
            return True
        if got is None:
            return False
        for i in members:
            outcomes[i] = self._outcome(jobs[i], got)
        return True

    # Pictures

    def _load(self, job: RecognizeJob) -> Any:
        from PIL import Image

        from meltify import imaging

        if job.kind == "page":
            return imaging.render_pdf_page(str(job.path), job.src.page or 1, self.opts.dpi)
        source = io.BytesIO(job.data) if job.data is not None else job.path
        with Image.open(source) as im:
            return imaging.flatten(im)

    def _prep(self, page: bool) -> str:
        from meltify import imaging

        sharp = "s" if self.opts.sharpen else ""
        if page:
            return f"dpi{self.opts.dpi}{sharp}"
        return f"auto{imaging.SHORT_TARGET}-{imaging.LONG_MAX}{sharp}"

    def picture(
        self, sha: str, load: Callable[[], Any], where: str, kind: str, compute: bool
    ) -> Picture | None:
        from meltify import imaging
        from meltify.engines import ocr as engines

        chosen = self.ocr_engines()
        if not chosen:
            raise MissingTool("ocr engine", "pip install ocrmac, or --engines NAME")
        lang, prep = self.opts.lang, self._prep(kind == "page")
        size: tuple[int, int] | None = None
        readings: dict[str, EngineReading] = {}
        missing = []
        for e in chosen:
            hit = self.cache.get("ocr", sha, e.name, lang, prep)
            if hit is None:
                missing.append(e)
                continue
            size = (hit["size"][0], hit["size"][1])
            boxes = [TextBox(t, tuple(b) if b else None, c) for t, b, c in hit["boxes"]]
            readings[e.name] = EngineReading(e.name, e.kind, boxes)
        if missing and not compute:
            return None
        if missing:
            loaded = attempt(load)
            if not loaded.ok:
                self.warn(f"can't open {where}: {loaded.error}")
                return Picture(size or (0, 0), list(readings.values()))
            image = loaded.value
            size = (image.width, image.height)
            factor = 1.0 if kind == "page" else imaging.auto_factor(*size)
            prepared = imaging.upscale(image, factor, self.opts.sharpen)
            prep_path = imaging.save(prepared, self.tmp / sha / "prepared.png")
            for e in missing:
                if isinstance(e, engines.Remote):
                    from meltify.commands.ocr import _read_tiles

                    got = attempt(_read_tiles, e, prepared, self.opts.tile_max, self.tmp / sha)
                else:
                    got = attempt(e.recognize, prep_path, prepared.size)
                if not got.ok:
                    self.warn(f"{e.name} failed on {where}: {got.error}")
                    continue
                boxes = [TextBox(b.text, _box(b.bbox, 1 / factor), b.conf) for b in got.value]
                self.cache.put(
                    {
                        "size": list(size),
                        "boxes": [
                            [b.text, list(b.bbox) if b.bbox else None, b.conf] for b in boxes
                        ],
                    },
                    "ocr",
                    sha,
                    e.name,
                    lang,
                    prep,
                )
                readings[e.name] = EngineReading(e.name, e.kind, boxes)
        assert size is not None
        return Picture(size, [readings[e.name] for e in chosen if e.name in readings])

    # Sound

    def _asr_prep(self, engine: Any) -> str:
        model = str(getattr(engine, "model", "") or "default")
        return Path(model).name[-48:]

    def speech(
        self, sha: str, path: Path | None, where: str, compute: bool
    ) -> list[Segment] | None:
        from meltify import ffmpeg

        engine = self.asr_engine()
        if engine is None:
            raise MissingTool("speech engine", "meltify doctor --install asr-mlx")
        key = ("asr", sha, engine.name, self.opts.lang, self._asr_prep(engine))
        if (hit := self.cache.get(*key)) is not None:
            return [Segment(s, e, t) for s, e, t in hit["segments"]]
        if not compute:
            return None
        assert path is not None
        segments: list[Segment] = []
        if _has_audio(path):
            wav = ffmpeg.audio(path, self.tmp / sha / "audio.wav", ffmpeg.Window())
            got = attempt(engine.transcribe, wav, self.opts.lang)
            if not got.ok:
                self.warn(f"{engine.name} failed on {where}: {got.error}")
                return []
            segments = got.value
        self.cache.put({"segments": [[s.start, s.end, s.text] for s in segments]}, *key)
        return segments

    def footage(
        self, sha: str, path: Path | None, where: str, listen: bool, compute: bool
    ) -> Footage | None:
        assert path is not None
        speech: list[Segment] | None = []
        if listen and self.asr_engine() is None:
            # Scenes still help without a transcript
            speech = None
        elif listen and (speech := self.speech(sha, path, where, compute)) is None:
            return None
        look = self.opts.frames > 0 and bool(self.ocr_engines())
        key = ("scene", sha, "ffmpeg", "any", f"t{self.opts.scene}-n{self.opts.frames}")
        if (listed := self.cache.get(*key)) is not None:
            frames = [
                Frame(t, s, self.picture(s, _unused, where, "image", False) if look else None)
                for t, s in listed["frames"]
            ]
            if not look or all(f.picture is not None for f in frames):
                return Footage(speech, listed["scenes"], frames)
        if not compute:
            return None
        scenes, shots = self._shots(path)
        self.cache.put({"scenes": scenes, "frames": [[t, s] for t, s, _ in shots]}, *key)
        frames = []
        for t, s, file in shots:
            pic = None
            if look:
                pic = self.picture(
                    s, lambda f=file: _open_flat(f), f"{where}{_span(t, t)}", "image", True
                )
            frames.append(Frame(t, s, pic))
        return Footage(speech, scenes, frames)

    def _shots(self, path: Path) -> tuple[list[float], list[tuple[float, str, Path]]]:
        """Scene times, and up to `frames` distinct frames spread across them"""
        from PIL import Image

        from meltify import ffmpeg, imaging

        folder = self.tmp / "frames" / hashlib.sha256(str(path).encode()).hexdigest()[:12]
        found = ffmpeg.scene_frames(path, folder, self.opts.scene, ffmpeg.Window())
        kept: list[tuple[Path, float]] = []
        last = None
        for file, t in found:
            with Image.open(file) as im:
                look = imaging.Look.of(im)
            # A scene that returns later still counts, so only compare with the last kept frame
            if last is not None and look.same_as(last, self.opts.dedup_distance):
                continue
            last = look
            kept.append((file, t))
        scenes = [t for _, t in kept]
        n = self.opts.frames
        if n <= 0:
            return scenes, []
        if len(kept) > n:
            step = (len(kept) - 1) / max(1, n - 1)
            kept = [kept[round(i * step)] for i in range(n)] if n > 1 else kept[:1]
        return scenes, [(t, hashlib.sha256(f.read_bytes()).hexdigest(), f) for f, t in kept]

    # Rendering

    def _outcome(self, job: RecognizeJob, got: Any) -> Outcome:
        if isinstance(got, Picture):
            return self._picture_outcome(job.src, got, self._placer(job, got.size))
        if isinstance(got, Footage):
            return self._footage_outcome(job.src, got)
        return self._speech_outcome(job.src, got)

    def _placer(self, job: RecognizeJob, size: tuple[int, int]) -> Callable[[Box | None], Src]:
        if job.kind == "page":
            k = 72 / self.opts.dpi
            return _placer(job.src, "pt", (0, 0, k, k))
        if job.rect is not None and size[0] and size[1]:
            x0, y0, x1, y1 = job.rect
            return _placer(job.src, "pt", (x0, y0, (x1 - x0) / size[0], (y1 - y0) / size[1]))
        return _placer(job.src, "px", (0, 0, 1, 1))

    def _picture_outcome(
        self, base: Src, pic: Picture, place: Callable[[Box | None], Src]
    ) -> Outcome:
        if not pic.readings:
            return Outcome(left="failed")
        body = next((r for r in pic.readings if r.boxes), None)
        if body is None:
            return Outcome()
        lines = []
        for b in body.boxes:
            loc = _where(place(b.bbox))
            lines.append(f"{loc}| {b.text}" if loc else b.text)
        disputed = []
        if len(pic.readings) == 1:
            lines.append("> unchecked: one engine")
        else:
            for v in compare(pic.readings, "numbers"):
                if v.agreed:
                    continue
                counts = ", ".join(f"{n} {c}" for n, c in v.counts.items())
                lines.append(f"> disputed {v.value}: {counts}")
                disputed.append(
                    finding(
                        place(v.bbox),
                        kind="disputed",
                        type="disputed",
                        value=v.value,
                        counts=v.counts,
                        text=f"{v.value}  ({counts})",
                    )
                )
        return Outcome([Block(base, "\n".join(lines))], disputed)

    def _speech_outcome(self, src: Src, segments: list[Segment]) -> Outcome:
        lines = [f"{_span(s.start, s.end)}| {s.text}" for s in segments if s.text]
        return Outcome([Block(src, "\n".join(lines))] if lines else [])

    def _footage_outcome(self, src: Src, got: Footage) -> Outcome:
        out = self._speech_outcome(src, got.speech or [])
        if got.speech is None:
            out.left = "engine"
        if got.scenes:
            line = "scenes: " + ", ".join(_clock(t) for t in got.scenes)
            if out.blocks:
                out.blocks[0].text += "\n" + line
            else:
                out.blocks.append(Block(src, line))
        for f in got.frames:
            if f.picture is None:
                continue
            at = replace(src, t=(f.t, f.t))
            shown = self._picture_outcome(at, f.picture, _placer(at, "px", (0, 0, 1, 1)))
            out.blocks += shown.blocks
            out.disputed += shown.disputed
        return out


def _unused() -> Any:
    raise AssertionError("cache-only lookups never load the image")


def _open_flat(path: Path) -> Any:
    from PIL import Image

    from meltify import imaging

    with Image.open(path) as im:
        return imaging.flatten(im)


def _has_audio(path: Path) -> bool:
    from meltify.ffmpeg import HINT
    from meltify.safe import require_binary, run

    probe = require_binary("ffprobe", HINT)
    out = run(
        [
            probe, "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
            "-of", "csv=p=0", "-i", str(path.resolve()),
        ]
    )  # fmt: skip
    return bool(out.stdout.strip())
