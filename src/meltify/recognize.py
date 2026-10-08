"""OCR and speech for the pixels and sound `read` finds, with results cited in place

A few workers read jobs side by side, while a local speech engine, which runs one large
model, still reads one job at a time. Identical content is read once, and every engine
result is cached by content hash under ${XDG_CACHE_HOME:-~/.cache}/meltify, so a rerun
only pays for what changed
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from meltify import paths
from meltify.config import pick
from meltify.converters import Block, RecognizeJob
from meltify.engines.asr import Segment
from meltify.engines.consensus import EngineReading, compare, disputed_row, tally
from meltify.engines.ocr import Reading, TextBox
from meltify.evidence import Src, clock, coordinate, finding, span
from meltify.ffmpeg import Window
from meltify.files import safe_name
from meltify.needs import ITEM_NOTE
from meltify.safe import MissingTool, Unreachable, attempt

# MissingTool names raised when no engine was found at all, which the run already warned
# about once
NO_ENGINE = ("ocr engine", "speech engine")
CACHE_VERSION = 1
# A model name goes into cache file names beside a 64-char hash, so only its tail is kept
MODEL_TAIL = 48
# Workers for uncached content. Image prep and Vision overlap well, and more of them would
# only queue on the engines that take one job at a time
WORKERS = 4

Box = tuple[float, float, float, float]


@dataclass(frozen=True)
class Options:
    engines: str = "auto"
    asr: str = "auto"
    lang: str = "ko"
    # Speech has its own setting, since OCR's lang forced on Whisper translates other speech
    asr_lang: str = "auto"
    budget: float = 600.0
    frames: int = 20
    refresh: bool = False
    dpi: int = 300
    # Resize factor for pictures, None to size each one toward SHORT_TARGET on its own
    upscale: float | None = None
    sharpen: bool = True
    equalize: bool = False
    tile_max: int = 2576
    # What engines are compared on: numbers or tokens
    compare: str = "numbers"
    # Lines read elsewhere, compared as one more engine on the run's single picture
    readings: tuple[Reading, ...] = ()
    scene: float = 0.3
    # Interval frames per second on top of scene changes, 0 for none
    fps: float = 0.0
    dedup_distance: int = 4
    keep_duplicates: bool = False
    window: Window = field(default_factory=Window)

    @classmethod
    def from_settings(
        cls,
        settings: Mapping[str, Any],
        *,
        engines: str | None = None,
        asr: str | None = None,
        budget: float | None = None,
        frames: int | None = None,
        refresh: bool = False,
        upscale: float | None = None,
        no_sharpen: bool = False,
        equalize: bool = False,
        compare: str = "numbers",
        readings: tuple[Reading, ...] = (),
        scene: float | None = None,
        fps: float | None = None,
        keep_duplicates: bool = False,
        window: Window | None = None,
    ) -> Options:
        """Options from the merged settings, where a flag left as None takes the setting"""
        read, ocr, media = settings["read"], settings["ocr"], settings["media"]
        factor = pick(upscale, ocr["upscale"])
        return cls(
            engines=str(pick(engines, ocr["engines"])),
            asr=str(pick(asr, settings["asr"]["engine"])),
            lang=str(settings["lang"]),
            asr_lang=str(settings["asr"]["lang"]),
            budget=float(pick(budget, read["budget"])),
            frames=int(pick(frames, read["frames"])),
            refresh=refresh,
            dpi=int(ocr["dpi"]),
            upscale=float(factor) or None,
            sharpen=bool(ocr["sharpen"]) and not no_sharpen,
            equalize=equalize,
            tile_max=int(ocr["tile_max"]),
            compare=compare,
            readings=readings,
            scene=float(pick(scene, media["scene"])),
            fps=float(pick(fps, media["fps"])),
            dedup_distance=int(media["dedup_distance"]),
            keep_duplicates=keep_duplicates,
            window=window or Window(),
        )


@dataclass
class Outcome:
    blocks: list[Block] = field(default_factory=list)
    disputed: list[dict[str, Any]] = field(default_factory=list)
    # A row per video frame looked at, its picture in the cache under `file`
    frames: list[dict[str, Any]] = field(default_factory=list)
    # Pictures only one engine read, so nothing in them was cross-checked
    unchecked: int = 0
    # Why the job is still pending: "budget", "engine" or "failed"
    left: str | None = None


@dataclass(frozen=True)
class Picture:
    size: tuple[int, int]  # source px, the space every box is stored in
    readings: list[EngineReading]


@dataclass(frozen=True)
class Shot:
    """A frame taken from a video, before any engine looks at it"""

    t: float
    sha: str
    # scene or interval, then the time of the frame it repeats when duplicates are kept
    reasons: tuple[str, ...]
    file: Path


@dataclass(frozen=True)
class Frame:
    shot: Shot
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
        name = safe_name(f"{sha}-{engine}-{lang}-{prep}")
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
            # Unique per thread, since two workers can store the same frame or picture
            tmp = target.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.write_text(json.dumps({"v": CACHE_VERSION, **value}, ensure_ascii=False), "utf-8")
            os.replace(tmp, target)
        except OSError:
            # A read-only cache only costs speed on the next run
            pass


def _identity(engine: Any) -> str:
    """The engine's name in cache keys, which changes with any setting that changes its output

    An HTTP engine's output depends on the model and the server behind it, so swapping either
    never reuses another's readings, which could otherwise pass one model's guess off as another's
    """
    from meltify.engines.asr import Api
    from meltify.engines.ocr import Remote

    if not isinstance(engine, Remote | Api):
        return engine.name
    prompt = engine.endpoint.prompt if engine.endpoint is not None else ""
    parts = "\n".join([engine.model, engine.base_url, prompt])
    return f"{engine.name}-{hashlib.sha256(parts.encode()).hexdigest()[:12]}"


def _box(b: Box | None, k: float) -> Box | None:
    return None if b is None else (b[0] * k, b[1] * k, b[2] * k, b[3] * k)


def _where(src: Src) -> str:
    # The box part of a cite, so each line reads as a suffix of its block's cite
    if src.bbox is None:
        return ""
    return f"@{src.unit}(" + ",".join(coordinate(v) for v in src.bbox) + ")"


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
        self.cache = Cache(paths.cache_dir(), opts.refresh)
        self.warned: set[str] = set()
        self._warn = warn
        self._ocr: list[Any] | None = None
        # Set by check_named for engines the command line named, which fail the run when
        # missing. One named only in a config file leaves its pictures listed instead
        self._ocr_named = False
        self._asr: Any = None
        self._asr_ready = False
        self._file_sha: dict[Path, str] = {}
        self.tmp = Path(tempfile.gettempdir())
        # Unique contents this run, and how many came straight from the cache
        self.seen = 0
        self.cached = 0
        # whisper.cpp runs one large model per call, so two at once only fight over the CPU
        self._speech_lock = threading.Lock()
        # Engines whose server stopped answering, skipped for the rest of the run
        self._down: set[str] = set()
        # Warnings a worker raises wait here, so they come out in job order
        self._held = threading.local()

    def warn(self, message: str) -> None:
        held = getattr(self._held, "warnings", None)
        if held is not None:
            held.append(message)
            return
        if message not in self.warned:
            self.warned.add(message)
            self._warn(message)

    def _one_at_a_time(self, engine: Any) -> Any:
        """The speech lock for a local engine, or a no-op for a server, which batches requests"""
        from meltify.engines.asr import Api

        return contextlib.nullcontext() if isinstance(engine, Api) else self._speech_lock

    # Engines

    def ocr_engines(self) -> list[Any]:
        if self._ocr is None:
            from meltify.engines import ocr as engines

            try:
                chosen = engines.ready(self.opts.engines, self.settings)
            except MissingTool as e:
                if self._ocr_named:
                    raise
                self.warn(f"{e}, so images stay listed as needs: {e.hint}")
                self._ocr = []
                return self._ocr
            # Body text comes from the first local engine that read anything, then the rest
            # in the order named
            rank = {n: i for i, n in enumerate(engines.LOCAL_ENGINES)}
            self._ocr = sorted(chosen, key=lambda e: rank.get(e.name, len(rank)))
            if self.opts.engines == "auto":
                for name, ep in engines.endpoints(self.settings).items():
                    if (why := ep.missing()) is not None:
                        self.warn(f"ocr endpoint {name} is skipped: {why}")
            if not chosen:
                self.warn(
                    "no local OCR engine, so images stay listed as needs."
                    f" {engines.INSTALL_HINT}, or --engines NAME"
                )
        return self._ocr

    def check_named(self, ocr: bool, asr: bool) -> None:
        """Raise MissingTool now for an engine the command line named, before anything is written

        Engines named only in a config file stay lazy, so a run with nothing to OCR still works.
        Endpoint tables are checked either way, so a typo fails before any output
        """
        from meltify.engines import asr as speech
        from meltify.engines import ocr as engines

        engines.endpoints(self.settings)
        speech.endpoints(self.settings)
        if ocr and self.opts.engines != "auto":
            self._ocr_named = True
            self.ocr_engines()
        if asr and self.opts.asr != "auto":
            self.asr_engine()

    def asr_engine(self) -> Any:
        """The speech engine, picked again when the one in use stopped answering"""
        if self._asr_ready and (self._asr is None or self._asr.name not in self._down):
            return self._asr
        from meltify.engines import asr

        first = not self._asr_ready
        self._asr_ready = True
        self._asr = None
        try:
            self._asr = asr.select(self.opts.asr, self.settings, frozenset(self._down))
        except MissingTool as e:
            if first and self.opts.asr != "auto":
                raise
            self.warn(f"no speech engine, so audio stays listed as needs: {e.hint}")
        if first and self.opts.asr == "auto":
            for name, ep in asr.endpoints(self.settings).items():
                if (why := ep.missing()) is not None:
                    self.warn(f"asr endpoint {name} is skipped: {why}")
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
        if job.kind == "page":
            from meltify.converters import render

            # A render embeds the time it was made, so its pages key on what it was drawn from
            base = render.drawn_from(job.path) or self.file_sha(job.path)
            return hashlib.sha256(f"{base}#p{job.src.page}".encode()).hexdigest()
        sha = self.file_sha(job.path)
        if job.src.frame is not None:
            return hashlib.sha256(f"{sha}#frame{job.src.frame}".encode()).hexdigest()
        return sha

    # The run

    def run(self, jobs: list[RecognizeJob]) -> list[Outcome]:
        """Outcomes in job order. Cached content comes first, then the rest within budget"""
        outcomes = [Outcome() for _ in jobs]
        if not jobs:
            return outcomes
        if any(
            j.kind in ("image", "page") or (j.kind == "video" and self.opts.frames > 0)
            for j in jobs
        ):
            self.ocr_engines()
        if any(j.kind in ("audio", "video") and j.listen for j in jobs):
            self.asr_engine()

        groups: dict[tuple[str, str, bool], list[int]] = {}
        for i, job in enumerate(jobs):
            listen = job.kind in ("audio", "video") and job.listen
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

            def read(group: tuple[str, str, bool], members: list[int]) -> list[str]:
                self._held.warnings = []
                try:
                    # Workers pick groups up in order, so the budget still goes to the first
                    if time.monotonic() - start >= self.opts.budget:
                        for i in members:
                            outcomes[i].left = "budget"
                    else:
                        self._settle(group, members, jobs, outcomes, compute=True)
                    return self._held.warnings
                finally:
                    self._held.warnings = None

            with ThreadPoolExecutor(min(WORKERS, max(1, len(pending)))) as pool:
                held = [pool.submit(contextvars.copy_context().run, read, *p) for p in pending]
                for warnings in [f.result() for f in held]:
                    for message in warnings:
                        self.warn(message)
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
                got: Any = self.picture(
                    sha, lambda: self._load(first), where, first.kind, compute, self.opts.readings
                )
            elif kind == "audio" and not listen:
                # Its subtitles already gave the transcript
                return True
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
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            # ffmpeg may refuse a file ffprobe opened, and that must not end the whole run
            self.warn(f"can't read {where}: {e}"[:ITEM_NOTE])
            for i in members:
                outcomes[i].left = "failed"
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
            from meltify.converters.run import LOCK

            # PyMuPDF isn't thread-safe, and OCR workers render pages side by side
            with LOCK:
                return imaging.render_pdf_page(str(job.path), job.src.page or 1, self.opts.dpi)
        source = io.BytesIO(job.data) if job.data is not None else job.path
        with Image.open(source) as im:
            if job.src.frame is not None:
                im.seek(job.src.frame - 1)
            return imaging.flatten(im)

    def _prep(self, page: bool) -> str:
        from meltify import imaging

        tone = ("s" if self.opts.sharpen else "") + ("e" if self.opts.equalize else "")
        if page:
            return f"dpi{self.opts.dpi}{tone}"
        if self.opts.upscale is not None:
            return f"x{self.opts.upscale:g}{tone}"
        return f"auto{imaging.SHORT_TARGET}-{imaging.LONG_MAX}{tone}"

    def picture(
        self,
        sha: str,
        load: Callable[[], Any],
        where: str,
        kind: str,
        compute: bool,
        given: tuple[Reading, ...] = (),
    ) -> Picture | None:
        """Every engine's lines for one picture, then the `given` readings, uncached"""
        from meltify import imaging
        from meltify.engines import ocr as engines

        chosen = [e for e in self.ocr_engines() if e.name not in self._down]
        if not chosen and not given:
            raise MissingTool("ocr engine", f"{engines.INSTALL_HINT}, or --engines NAME")
        lang, prep = self.opts.lang, self._prep(kind == "page")
        size: tuple[int, int] | None = None
        readings: dict[str, EngineReading] = {}
        missing = []
        for e in chosen:
            hit = self.cache.get("ocr", sha, _identity(e), lang, prep)
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
            factor = 1.0 if kind == "page" else self.opts.upscale or imaging.auto_factor(*size)
            prepared = imaging.upscale(image, factor, self.opts.sharpen)
            if self.opts.equalize:
                prepared = imaging.equalize(prepared)
            # A folder per read, since a frame and a picture can share content and workers
            work = Path(tempfile.mkdtemp(prefix=f"{sha[:12]}-", dir=self.tmp))
            prep_path = imaging.save(prepared, work / "prepared.png")
            for e in missing:
                try:
                    if isinstance(e, engines.Remote):
                        got = attempt(engines.read_tiles, e, prepared, self.opts.tile_max, work)
                    else:
                        got = attempt(e.recognize, prep_path, prepared.size)
                except Unreachable as err:
                    self._drop(e.name, err)
                    continue
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
                    _identity(e),
                    lang,
                    prep,
                )
                readings[e.name] = EngineReading(e.name, e.kind, boxes)
        found = [readings[e.name] for e in chosen if e.name in readings]
        found += [EngineReading(r.name, r.kind, r.recognize(Path(), (0, 0))) for r in given]
        return Picture(size or (0, 0), found)

    def _drop(self, name: str, err: Unreachable) -> None:
        # A server that's down fails every call after a timeout, so ask it once per run
        self._down.add(name)
        self.warn(f"{name} is skipped for the rest of this run: {err}")

    # Sound

    def _asr_prep(self, engine: Any) -> str:
        model = str(getattr(engine, "model", "") or "default")
        return Path(model).name[-MODEL_TAIL:] + self._window_tag()

    def _window_tag(self) -> str:
        w = self.opts.window
        if w.start is None and w.end is None:
            return ""
        return f"-w{w.start or 0:g}-{'end' if w.end is None else f'{w.end:g}'}"

    def speech(
        self, sha: str, path: Path | None, where: str, compute: bool
    ) -> list[Segment] | None:
        from meltify import ffmpeg
        from meltify.engines import asr

        engine = self.asr_engine()
        if engine is None:
            raise MissingTool("speech engine", asr.INSTALL_HINT)
        key = ("asr", sha, _identity(engine), self.opts.asr_lang, self._asr_prep(engine))
        if (hit := self.cache.get(*key)) is not None:
            return [Segment(s, e, t) for s, e, t in hit["segments"]]
        if not compute:
            return None
        assert path is not None
        segments: list[Segment] = []
        if ffmpeg.has_audio(path):
            work = Path(tempfile.mkdtemp(prefix=f"{sha[:12]}-", dir=self.tmp))
            window = self.opts.window
            wav = ffmpeg.audio(path, work / "audio.wav", window)
            try:
                with self._one_at_a_time(engine):
                    got = attempt(engine.transcribe, wav, self.opts.asr_lang)
            except Unreachable as err:
                # The next engine in line takes this recording, or it stays a need
                self._drop(engine.name, err)
                return self.speech(sha, path, where, compute)
            if not got.ok:
                self.warn(f"{engine.name} failed on {where}: {got.error}")
                return []
            # Cut audio starts at zero, so the window start puts times back on the recording
            segments = [
                Segment(s.start + window.offset(), s.end + window.offset(), s.text)
                for s in got.value
            ]
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
        elif listen:
            try:
                if (speech := self.speech(sha, path, where, compute)) is None:
                    return None
            except MissingTool as e:
                # The speech server went down mid-run, and scenes still help
                if e.name not in NO_ENGINE:
                    raise
                speech = None
        look = self.opts.frames > 0 and bool(self.ocr_engines())
        o = self.opts
        key = (
            "scene",
            sha,
            "ffmpeg",
            "any",
            # r2: listings hold each frame's reasons and the total before the cap
            f"r2-t{o.scene}-n{o.frames}-f{o.fps:g}-k{int(o.keep_duplicates)}"
            f"-d{o.dedup_distance}{self._window_tag()}",
        )
        listed = self.cache.get(*key)
        if listed is not None:
            shots = [Shot(t, h, tuple(r), self.frame_file(h)) for t, h, r in listed["frames"]]
            frames = [
                Frame(
                    shot, self.picture(shot.sha, _unused, where, "image", False) if look else None
                )
                for shot in shots
            ]
            # A frame picture cleared from the cache means a fresh pass over the video
            if all(sh.file.is_file() for sh in shots) and (
                not look or all(f.picture is not None for f in frames)
            ):
                self._capped(where, len(frames), listed["total"])
                return Footage(speech, listed["scenes"], frames)
        if not compute:
            return None
        scenes, shots, total = self._shots(path)
        shots = [replace(sh, file=self._store_frame(sh.sha, sh.file)) for sh in shots]
        listing = [[sh.t, sh.sha, list(sh.reasons)] for sh in shots]
        self.cache.put({"scenes": scenes, "frames": listing, "total": total}, *key)
        self._capped(where, len(shots), total)
        frames = []
        for sh in shots:
            pic = None
            if look:
                pic = self.picture(
                    sh.sha,
                    lambda f=sh.file: _open_flat(f),
                    f"{where}{span(sh.t, sh.t)}",
                    "image",
                    True,
                )
            frames.append(Frame(sh, pic))
        return Footage(speech, scenes, frames)

    def _capped(self, where: str, kept: int, total: int) -> None:
        if 0 < kept < total:
            self.warn(f"{where}: kept {kept} of {total} frames, raise --frames to keep more")

    def frame_file(self, sha: str) -> Path:
        return self.cache.root / "frame" / sha[:2] / f"{sha}.jpg"

    def _store_frame(self, sha: str, file: Path) -> Path:
        """The frame's picture kept in the cache by content, so a cached run still has it"""
        target = self.frame_file(sha)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.is_file():
                # Unique per thread and swapped in whole, since two videos can share a frame
                # and a reader must never see half a JPEG
                tmp = target.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
                shutil.copyfile(file, tmp)
                os.replace(tmp, target)
            return target
        except OSError:
            # A read-only cache keeps the picture where the run removes it after the
            # markdown is out, since this recognizer's temp folder goes first
            from meltify.converters import run

            kept = run.workdir("meltify-frames-") / file.name
            shutil.copyfile(file, kept)
            return kept

    def _shots(self, path: Path) -> tuple[list[float], list[Shot], int]:
        """Scene times, the frames to keep, and how many there were before the cap

        Up to `frames` are kept, spread across scene changes and intervals
        """
        from meltify import ffmpeg, imaging

        o = self.opts
        folder = Path(tempfile.mkdtemp(prefix="frames-", dir=self.tmp))
        taken = [(f, t, "scene") for f, t in ffmpeg.scene_frames(path, folder, o.scene, o.window)]
        # Interval frames only matter as frames, while scene times also feed the scenes line
        if o.fps > 0 and o.frames > 0:
            taken += [
                (f, t, "interval") for f, t in ffmpeg.interval_frames(path, folder, o.fps, o.window)
            ]
        # A scene frame goes before an interval frame of the same moment
        taken.sort(key=lambda x: (x[1], x[2] != "scene"))
        when = {f: (t, why) for f, t, why in taken}
        kept: list[tuple[Path, float, tuple[str, ...]]] = []
        for f, repeats in imaging.distinct_frames(
            [f for f, _, _ in taken], o.dedup_distance, o.keep_duplicates
        ):
            t, why = when[f]
            again = () if repeats is None else (f"repeats {clock(when[repeats][0])}",)
            kept.append((f, t, (why, *again)))
        fresh = [k for k in kept if len(k[2]) == 1]
        scenes = [t for _, t, reasons in fresh if reasons == ("scene",)]
        total, n = len(kept), o.frames
        if n <= 0:
            return scenes, [], total
        if total > n:
            # Distinct frames come first, so repeats kept with --keep-duplicates only fill
            # what's left of the cap instead of crowding out a scene
            repeats = [k for k in kept if len(k[2]) > 1]
            kept = _spread(fresh, n) + _spread(repeats, n - min(n, len(fresh)))
            kept.sort(key=lambda k: k[1])
        shots = [Shot(t, hashlib.sha256(f.read_bytes()).hexdigest(), r, f) for f, t, r in kept]
        return scenes, shots, total

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
            for v in compare(pic.readings, self.opts.compare):
                if v.agreed:
                    continue
                lines.append(f"> disputed {v.value}: {tally(v)}")
                disputed.append(disputed_row(place(v.bbox), v))
        return Outcome([Block(base, "\n".join(lines))], disputed, unchecked=len(pic.readings) == 1)

    def _speech_outcome(self, src: Src, segments: list[Segment]) -> Outcome:
        lines = [f"{span(s.start, s.end)}| {s.text}" for s in segments if s.text]
        if not lines:
            return Outcome()
        engine = self._asr.name if self._asr is not None else "speech"
        return Outcome([Block(src, "\n".join([f"> transcribed by {engine}", *lines]))])

    def _footage_outcome(self, src: Src, got: Footage) -> Outcome:
        out = self._speech_outcome(src, got.speech or [])
        if got.speech is None:
            out.left = "engine"
        if got.scenes:
            line = "scenes: " + ", ".join(clock(t) for t in got.scenes)
            if out.blocks:
                out.blocks[0].text += "\n" + line
            else:
                out.blocks.append(Block(src, line))
        for f in got.frames:
            at = replace(src, t=(f.shot.t, f.shot.t))
            out.frames.append(
                finding(at, kind="frame", reasons=list(f.shot.reasons), file=str(f.shot.file))
            )
            if f.picture is None:
                continue
            shown = self._picture_outcome(at, f.picture, _placer(at, "px", (0, 0, 1, 1)))
            out.blocks += shown.blocks
            out.disputed += shown.disputed
            out.unchecked += shown.unchecked
        return out


def _spread(items: list[Any], n: int) -> list[Any]:
    """`n` of `items` spaced evenly from first to last"""
    if n <= 0:
        return []
    if len(items) <= n:
        return items
    if n == 1:
        return items[:1]
    step = (len(items) - 1) / (n - 1)
    return [items[round(i * step)] for i in range(n)]


def _unused() -> Any:
    raise AssertionError("cache-only lookups never load the image")


def _open_flat(path: Path) -> Any:
    from PIL import Image

    from meltify import imaging

    with Image.open(path) as im:
        return imaging.flatten(im)
