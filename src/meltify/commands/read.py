from __future__ import annotations

import argparse
import importlib
import re
import shutil
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meltify import __version__
from meltify.converters import Block, Converted, RecognizeJob
from meltify.evidence import Envelope, Src, finding
from meltify.files import common_root, flat_name, flat_names, iter_files, unique_name
from meltify.output import append_jsonl
from meltify.safe import MissingTool

NAME = "read"
HELP = (
    "melt files, folders and URLs of any format into cited markdown,"
    " with images and recordings read by local OCR and speech engines"
)
COLUMNS = ["kind", "chars", "needs", "out", "cite"]

# How many container levels to follow, archives and attachments counted together
MAX_DEPTH = 3
# Pending reasons in the order they show up in a row's needs. Missing engines keep the
# plain 0.1 wording, since installing one is the fix
LEFT = {"engine": "", "budget": " (budget)", "failed": " (failed)"}
# Converters that open documents with PyMuPDF, which isn't thread-safe. legacy renders
# .ppt to PDF and reads it with the pdf converter
PYMUPDF = {"pdf", "legacy"}


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("paths", nargs="+", help="files, folders or http(s) URLs")
    p.add_argument("--jobs", type=int, help="parallel workers for non-PDF files")
    p.add_argument(
        "--shallow",
        action="store_true",
        help="skip only OCR and speech recognition, listing images, scans and recordings instead",
    )
    p.add_argument(
        "--budget",
        type=float,
        metavar="SEC",
        help="seconds to spend on uncached OCR and speech (default: read.budget, 600)",
    )
    p.add_argument(
        "--frames",
        type=int,
        metavar="N",
        help="scene frames to OCR per video, 0 for none (default: read.frames, 20)",
    )
    p.add_argument(
        "--engines",
        help="OCR engines like vision,paddle or gemini (default: auto, local engines only)",
    )
    p.add_argument("--asr", help="mlx, whispercpp or api (default: asr.engine, local only)")
    p.add_argument("--refresh", action="store_true", help="ignore cached OCR, speech and fetches")
    web = p.add_argument_group("URL inputs")
    web.add_argument("--render", action="store_true", help="render pages in a headless browser")
    web.add_argument(
        "--whole", action="store_true", help="keep the whole page, not just the article"
    )
    web.add_argument(
        "--allow-private", action="store_true", help="allow URLs that resolve to private addresses"
    )
    web.add_argument(
        "--ignore-robots", action="store_true", help="fetch even if robots.txt says no"
    )
    web.add_argument("--max-bytes", type=int, help="largest download in bytes (default: 20000000)")


def is_url(raw: str) -> bool:
    return raw.lower().startswith(("http://", "https://"))


def member_name(raw: str | None, index: int) -> str:
    # Senders and archive authors pick these names, so `..`, roots and drive letters must
    # never reach a path. Plain directories stay, since they tell archive members apart
    segments = (raw or "").replace("\\", "/").split("/")
    if segments and re.fullmatch(r"[A-Za-z]:", segments[0]):
        segments = segments[1:]
    kept = [s.strip() for s in segments if s.strip() and set(s.strip()) != {"."}]
    return "/".join(kept) or f"attachment-{index}"


def fresh(name: str, taken: set[str], index: int) -> str:
    # Keep the suffix last, so the renamed copy still picks the same converter
    while name in taken:
        stem, dot, suffix = name.rpartition(".")
        name = f"{stem}-{index}.{suffix}" if dot and stem else f"{name}-{index}"
        index += 1
    taken.add(name)
    return name


def job_needs(jobs: list[RecognizeJob]) -> list[str]:
    images = sum(j.kind == "image" for j in jobs)
    pages = [j.src.page for j in jobs if j.kind == "page"]
    needs = []
    if pages:
        needs.append("ocr pages " + ",".join(map(str, pages)))
    if images:
        needs.append("ocr" if images == 1 else f"ocr {images} images")
    if any(j.kind in ("audio", "video") for j in jobs):
        needs.append("media")
    return needs


def place(blocks: list[Block], extra: list[Block]) -> list[Block]:
    """Blocks with recognized text slotted after the page, slide or sheet they belong to"""
    sheets = list(dict.fromkeys(b.src.sheet for b in blocks if b.src.sheet))

    def order(src: Src) -> tuple[int, int, int]:
        sheet = 0
        if src.sheet:
            sheet = sheets.index(src.sheet) + 1 if src.sheet in sheets else len(sheets) + 1
        return (src.page or 0, src.slide or 0, sheet)

    out = list(blocks)
    for b in extra:
        at = order(b.src)
        out.insert(next((i for i, x in enumerate(out) if order(x.src) > at), len(out)), b)
    return out


@dataclass
class Output:
    """A converted item waiting for the recognition stage before its markdown is written"""

    converted: Converted
    target: Path
    origin: str
    row: dict[str, Any]


class Reader:
    def __init__(
        self,
        out_dir: Path,
        names: dict[Path, str],
        *,
        shallow: bool = False,
        fetch_opts: dict[str, Any] | None = None,
        whole: bool = False,
        media_conf: dict[str, Any] | None = None,
    ) -> None:
        self.out_dir = out_dir
        self.names = names
        self.taken = set(names.values())
        self.shallow = shallow
        self.fetch_opts = fetch_opts or {}
        self.whole = whole
        self.media_conf = media_conf or {}
        self.outputs: list[Output] = []
        # URL media whose subtitles already gave the transcript
        self.transcribed: set[Src] = set()
        # PyMuPDF isn't thread-safe, and nested items can be PDFs too
        self.pdf_lock = threading.Lock()
        self.name_lock = threading.Lock()
        self.out_lock = threading.Lock()

    def _convert(
        self, kind: str, convert: Callable[..., Converted], path: Path, src: Src
    ) -> Converted:
        if kind in PYMUPDF:
            with self.pdf_lock:
                return convert(path, src)
        return convert(path, src)

    def one(
        self, path: Path, src: Src, name: str | None = None, depth: int = 0
    ) -> list[dict[str, Any]]:
        from meltify.converters import pick

        kind, convert = pick(path)
        row = finding(src, kind=kind, chars=0, needs=[], hidden=0, out=None)
        try:
            converted = self._convert(kind, convert, path, src)
        except MissingTool as e:
            return [{**row, "error": str(e), "hint": e.hint, "needs": [e.name]}]
        except Exception as e:  # noqa: BLE001
            # One unreadable file shouldn't stop the rest of the folder
            return [{**row, "error": f"{type(e).__name__}: {e}"[:300]}]
        return self.keep(converted, name or self.names[path], depth, row, src.cite())

    def keep(
        self,
        converted: Converted,
        name: str,
        depth: int,
        row: dict[str, Any],
        origin: str,
    ) -> list[dict[str, Any]]:
        """Queue the converted item for rendering and melt its children"""
        if self.shallow:
            # 0.1 never looked inside documents for pictures
            converted.jobs = [j for j in converted.jobs if j.src.img is None]
        target = self.out_dir / f"{name}.md"
        row.update(
            kind=converted.kind,
            needs=list(converted.needs),
            hidden=converted.hidden,
            out=str(target),
        )
        with self.out_lock:
            self.outputs.append(Output(converted, target, origin, row))
        rows = [row]
        if not converted.children:
            return rows
        if depth >= MAX_DEPTH:
            row["needs"].append(f"{len(converted.children)} nested items beyond depth {MAX_DEPTH}")
            return rows
        members: set[str] = set()
        stored: set[str] = set()
        for i, child in enumerate(converted.children, start=1):
            member = fresh(member_name(child.name, i), members, i)
            saved = (
                self.out_dir
                / "attachments"
                / name
                / fresh(flat_name(Path(member.replace("/", "__"))), stored, i)
            )
            saved.parent.mkdir(parents=True, exist_ok=True)
            if child.path is not None:
                shutil.move(child.path, saved)
            else:
                saved.write_bytes(child.data or b"")
            child_src = child.parent.inside(member)
            with self.name_lock:
                child_out = unique_name(
                    flat_name(Path(f"{name}__{member.replace('/', '__')}")),
                    self.taken,
                    child_src.cite(),
                )
            rows += self.one(saved, child_src, child_out, depth + 1)
        return rows

    def url(self, url: str) -> list[dict[str, Any]]:
        from meltify.commands.media import _work_name

        src = Src(url)
        row = finding(src, kind="url", chars=0, needs=[], hidden=0, out=None)
        try:
            fetch = importlib.import_module("meltify.fetch")
            web = importlib.import_module("meltify.converters.web")
        except ImportError:
            return [{**row, "needs": ["url support missing"]}]
        with self.name_lock:
            name = unique_name(_work_name(url), self.taken, url)
        try:
            got = fetch.fetch(url, self.out_dir / "web", fetch.FetchOptions(**self.fetch_opts))
            meta = {
                "fetched_at": got.fetched_at,
                "final_url": got.final_url,
                "sha256": got.sha256,
                "etag": got.etag,
            }
            row.update(meta)
            if got.kind == "media":
                converted = self._media(url, src, self.out_dir / "web" / name)
            elif got.kind == "html":
                src = Src(got.final_url)
                converted = web.convert_page(got.path, src, whole=self.whole)
                row.update(finding(src))
            else:
                from meltify.converters import pick

                row["kind"], convert = pick(got.path)
                converted = self._convert(row["kind"], convert, got.path, src)
        except MissingTool as e:
            return [{**row, "error": str(e), "hint": e.hint, "needs": [e.name]}]
        except Exception as e:  # noqa: BLE001
            return [{**row, "error": f"{type(e).__name__}: {e}"[:300]}]
        origin = " ".join([src.cite(), *(f"{k}={v}" for k, v in meta.items() if v)])
        return self.keep(converted, name, 0, row, origin)

    def _media(self, url: str, src: Src, work: Path) -> Converted:
        from meltify.commands.media import _download, parse_subtitles
        from meltify.recognize import _span

        if self.shallow:
            return Converted("media", needs=["media"])
        video, subs = _download(
            url,
            work,
            list(self.media_conf.get("sub_langs", ["ko", "en"])),
            False,
            int(self.media_conf.get("max_height", 1080)),
        )
        out = Converted("media")
        for sub in subs:
            cues = parse_subtitles(sub.read_text("utf-8", errors="replace"))
            if cues:
                # Subtitles are free and exact, so they stand in for speech recognition
                lines = [f"{_span(s, e)}| {text}" for s, e, text in cues]
                out.blocks.append(Block(src, "\n".join(lines)))
                self.transcribed.add(src)
                break
        if video is not None:
            out.jobs.append(RecognizeJob("video", src, path=video))
        return out

    def finish(self, recognize: Callable[..., list[Any]] | None) -> list[dict[str, Any]]:
        """Fold recognized text into each item, write its markdown and return disputed rows"""
        jobs = [j for o in self.outputs for j in o.converted.jobs]
        outcomes = recognize(jobs, self.transcribed) if recognize and jobs else None
        disputed: list[dict[str, Any]] = []
        n = 0
        for o in self.outputs:
            mine = o.converted.jobs
            got = outcomes[n : n + len(mine)] if outcomes is not None else None
            n += len(mine)
            if got is None:
                left = job_needs(mine)
            else:
                left = [
                    need + suffix
                    for reason, suffix in LEFT.items()
                    for need in job_needs(
                        [j for j, g in zip(mine, got, strict=True) if g.left == reason]
                    )
                ]
                o.converted.blocks = place(o.converted.blocks, [b for g in got for b in g.blocks])
                for g in got:
                    disputed += [
                        {**d, "chars": 0, "needs": [], "out": str(o.target)} for d in g.disputed
                    ]
            o.converted.needs = left + o.converted.needs
            o.row["needs"] = left + o.row["needs"]
            o.row["chars"] = o.converted.chars
            o.target.parent.mkdir(parents=True, exist_ok=True)
            o.target.write_text(o.converted.markdown(o.origin), encoding="utf-8")
        return disputed


def run(args: argparse.Namespace, settings: dict[str, Any]) -> Envelope:
    from meltify.recognize import Options, Recognizer

    env = Envelope(command=NAME, version=__version__)
    conf = settings.get("read", {})
    ocr_conf = settings.get("ocr", {})
    media_conf = settings.get("media", {})
    out_dir = Path(settings["out_dir"]) / "read"
    urls = [p for p in args.paths if is_url(p)]
    local = [Path(p) for p in args.paths if not is_url(p)]
    files = list(iter_files(local))
    reader = Reader(
        out_dir,
        flat_names(files, common_root(local)),
        shallow=args.shallow,
        fetch_opts={
            "max_bytes": args.max_bytes or int(conf.get("max_bytes", 20_000_000)),
            "allow_private": args.allow_private,
            "ignore_robots": args.ignore_robots,
            "refresh": args.refresh,
            "render": args.render,
        },
        whole=args.whole,
        media_conf=media_conf,
    )
    env.inputs = [{"path": str(p)} for p in args.paths]

    tasks: list[Callable[[], list[dict[str, Any]]]] = [
        (lambda f=f: reader.one(f, Src(str(f)))) for f in files
    ]
    tasks += [(lambda u=u: reader.url(u)) for u in urls]
    rows: list[dict[str, Any]] = []
    jobs = args.jobs or int(conf.get("jobs", 8))
    with ThreadPoolExecutor(max(1, jobs)) as pool:
        for result in pool.map(lambda task: task(), tasks):
            rows += result

    recognizer = None
    if not args.shallow:
        opts = Options(
            engines=args.engines or str(ocr_conf.get("engines", "auto")),
            asr=args.asr or str(settings.get("asr", {}).get("engine", "auto")),
            lang=str(settings.get("lang", "ko")),
            budget=float(args.budget if args.budget is not None else conf.get("budget", 600)),
            frames=int(args.frames if args.frames is not None else conf.get("frames", 20)),
            refresh=args.refresh,
            dpi=int(ocr_conf.get("dpi", 300)),
            sharpen=bool(ocr_conf.get("sharpen", True)),
            tile_max=int(ocr_conf.get("tile_max", 2576)),
            scene=float(media_conf.get("scene", 0.3)),
            dedup_distance=int(media_conf.get("dedup_distance", 4)),
        )
        recognizer = Recognizer(opts, settings, env.warnings.append)
    started = time.monotonic()
    rows += reader.finish(recognizer.run if recognizer else None)
    rows.sort(key=lambda r: r["cite"])

    index = out_dir / "index.jsonl"
    index.unlink(missing_ok=True)
    append_jsonl(index, rows)
    env.results = rows
    env.artifact(str(index), "results")

    errors = [r for r in rows if r.get("error")]
    pending = [r for r in rows if r["needs"]]
    disputed = sum(r.get("type") == "disputed" for r in rows)
    env.summary = (
        f"{len(rows) - disputed} items melted into {out_dir}, "
        f"{len(errors)} errors, {len(pending)} need more work"
    )
    if recognizer is not None and recognizer.seen:
        env.summary += (
            f", {recognizer.seen} images and recordings read in"
            f" {time.monotonic() - started:.1f}s ({recognizer.cached} cached),"
            f" {disputed} disputed values"
        )
    for r in errors:
        env.warnings.append(
            f"{r['cite']}: {r['error']}" + (f" ({r['hint']})" if r.get("hint") else "")
        )
    return env
