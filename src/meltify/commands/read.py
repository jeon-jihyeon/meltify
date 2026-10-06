from __future__ import annotations

import argparse
import contextvars
import importlib
import multiprocessing
import os
import pickle
import shutil
import sys
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meltify import __version__, passwords
from meltify.converters import Block, Converted, RecognizeJob, place
from meltify.evidence import Envelope, Src, finding
from meltify.files import (
    common_root,
    flat_name,
    flat_names,
    iter_files,
    member_name,
    unique_name,
)
from meltify.needs import error_note
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
# What a per-file boundary lets through. Anything else a parser raises, like the
# BaseException pyo3 turns a Rust panic into, fails only that file
FATAL = (KeyboardInterrupt, SystemExit, GeneratorExit)
# Converters that open documents with PyMuPDF, which isn't thread-safe. Those that start
# soffice first, like legacy for .ppt, take the lock themselves only around PyMuPDF
PYMUPDF = {"pdf"}
# Embedded pictures bigger than this wait for OCR in a temp file instead of in memory
SPILL_BYTES = 256 << 10


def add_arguments(p: argparse.ArgumentParser) -> dict[str, argparse.Action]:
    """Add the read flags to `p`, returning them by dest

    The Python API checks and converts its keywords through the same flags
    """
    actions: dict[str, argparse.Action] = {}

    def add(group: Any, *flags: str, **options: Any) -> None:
        action = group.add_argument(*flags, **options)
        actions[action.dest] = action

    add(p, "paths", nargs="+", help="files, folders or http(s) URLs")
    add(
        p,
        "--jobs",
        type=int,
        help="files to convert at once, PDFs in worker processes (default: read.jobs, 8)",
    )
    add(
        p,
        "--shallow",
        action="store_true",
        help="skip OCR, speech, and drawing or recalculation work, listing what was skipped",
    )
    add(
        p,
        "--budget",
        type=float,
        metavar="SEC",
        help="seconds to spend on uncached OCR and speech (default: read.budget, 600)",
    )
    add(
        p,
        "--frames",
        type=int,
        metavar="N",
        help="scene frames to OCR per video, 0 for none (default: read.frames, 20)",
    )
    add(
        p,
        "--engines",
        help="OCR engines like vision,paddle or gemini (default: auto, local engines only)",
    )
    add(p, "--asr", help="mlx, whispercpp or api (default: asr.engine, local only)")
    add(p, "--refresh", action="store_true", help="ignore cached OCR, speech and fetches")
    web = p.add_argument_group("URL inputs")
    add(web, "--render", action="store_true", help="render pages in a headless browser")
    add(web, "--whole", action="store_true", help="keep the whole page, not just the article")
    add(
        web,
        "--allow-private",
        action="store_true",
        help="allow URLs that resolve to private addresses",
    )
    add(web, "--ignore-robots", action="store_true", help="fetch even if robots.txt says no")
    add(web, "--max-bytes", type=int, help="largest download in bytes (default: 20000000)")
    locked = p.add_argument_group("encrypted inputs")
    add(
        locked,
        "--password-file",
        type=Path,
        metavar="PATH",
        help="password for encrypted files, the first line of PATH (or set MELTIFY_PASSWORD)",
    )
    add(
        locked,
        "--password",
        help="password on the command line, visible to other users of this machine",
    )
    return actions


def is_url(raw: str) -> bool:
    return raw.lower().startswith(("http://", "https://"))


def fresh(name: str, taken: set[str], index: int, fold: bool = False) -> str:
    # Keep the suffix last, so the renamed copy still picks the same converter. Names bound
    # for disk fold case, since macOS and Windows see Readme.txt and README.txt as one file
    key = str.casefold if fold else str
    while key(name) in taken:
        stem, dot, suffix = name.rpartition(".")
        name = f"{stem}-{index}.{suffix}" if dot and stem else f"{name}-{index}"
        index += 1
    taken.add(key(name))
    return name


def given_password(
    args: argparse.Namespace, warn: Callable[[str], None], secret: str | None = None
) -> str | None:
    # A password handed over in Python never touched argv, so it skips the warning below
    if secret:
        return secret
    if args.password:
        # Anyone on the machine can read argv from the process list
        warn("--password is visible to other users, prefer --password-file or MELTIFY_PASSWORD")
    if args.password_file:
        # utf-8-sig drops the byte order mark Windows Notepad writes
        first = next(iter(args.password_file.read_text("utf-8-sig").splitlines()), "")
        if not first:
            raise ValueError(f"no password on the first line of {args.password_file}")
        return first
    # A password given for this run outranks the one the shell carries for every run
    return args.password or os.environ.get("MELTIFY_PASSWORD")


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


def _melt_pdf(path: Path, src: Src) -> Converted:
    """pdf's convert in a worker process, failing with an error the parent can unpickle"""
    from meltify.converters import pdf

    try:
        return pdf.convert(path, src)
    except FATAL:
        raise
    except BaseException as e:  # noqa: BLE001
        try:
            pickle.loads(pickle.dumps(e))
        except Exception:  # noqa: BLE001
            raise RuntimeError(f"{type(e).__name__}: {e}") from None
        raise


def _spawn_safe() -> bool:
    """Whether a spawned process can start without running the caller's script again

    Spawn imports the main module of the parent in every child. multiprocessing skips a
    module run with -m, and there's nothing to import from a REPL or `python -c`. The
    meltify console script only calls main under a `__name__` guard. Any other script
    may do its work at import time, so PDFs then stay in this process
    """
    main = sys.modules.get("__main__")
    name = getattr(getattr(main, "__spec__", None), "name", None) or ""
    if name == "__main__" or name.endswith(".__main__"):
        return True
    if getattr(main, "__file__", None) is None:
        return True
    from meltify import cli

    return getattr(main, "main", None) is cli.main


class PdfPool:
    """Worker processes for PDFs, since PyMuPDF is only unsafe across threads

    Each process holds its own MuPDF, so PDFs convert in parallel instead of one at a time
    under the shared lock. pdf's convert reads nothing but the path and src, as unlock
    already swapped an encrypted file for its decrypted copy. A pool that can't start or
    breaks hands its PDFs back to the lock
    """

    def __init__(self, workers: int) -> None:
        self.workers = workers
        self.executor: ProcessPoolExecutor | None = None
        self.broken = False
        self.lock = threading.Lock()

    @classmethod
    @contextmanager
    def start(cls, jobs: int) -> Iterator[PdfPool | None]:
        if jobs <= 1 or not _spawn_safe():
            yield None
            return
        pool = cls(min(jobs, os.cpu_count() or 1))
        try:
            yield pool
        finally:
            pool.close()

    def convert(self, convert: Callable[..., Converted], path: Path, src: Src) -> Converted | None:
        """The converted PDF, or None when this file has to go through the lock"""
        from meltify.converters import pdf

        # A converter a test or plugin swapped in may not pickle
        if convert is not pdf.convert or (executor := self._executor()) is None:
            return None
        try:
            task = executor.submit(_melt_pdf, path, src)
        except (BrokenProcessPool, RuntimeError):
            # A broken or closed pool refuses work
            self.broken = True
            return None
        try:
            return task.result()
        except BrokenProcessPool:
            # A worker that crashed takes the whole pool down
            self.broken = True
            return None

    def _executor(self) -> ProcessPoolExecutor | None:
        with self.lock:
            if self.executor is None and not self.broken:
                try:
                    self.executor = ProcessPoolExecutor(
                        self.workers, mp_context=multiprocessing.get_context("spawn")
                    )
                except (OSError, ValueError, NotImplementedError):
                    self.broken = True
            return None if self.broken else self.executor

    def close(self) -> None:
        if self.executor is not None:
            self.executor.shutdown(wait=True, cancel_futures=True)


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
        # Folded the way unique_name compares them
        self.taken = {n.casefold() for n in names.values()}
        self.shallow = shallow
        self.fetch_opts = fetch_opts or {}
        self.whole = whole
        self.media_conf = media_conf or {}
        self.outputs: list[Output] = []
        # URL media whose subtitles already gave the transcript
        self.transcribed: set[Src] = set()
        from meltify.converters.run import LOCK

        # PyMuPDF isn't thread-safe, and nested items can be PDFs too. Shared with the
        # renderer, which rasterizes pages for converters outside PYMUPDF
        self.pdf_lock = LOCK
        self.name_lock = threading.Lock()
        self.out_lock = threading.Lock()
        # Set by the run. Nested items go to `pool` as their own tasks, and PDFs to
        # `pdf_pool`, whose processes each hold their own PyMuPDF
        self.pool: ThreadPoolExecutor | None = None
        self.pdf_pool: PdfPool | None = None
        self.futures: list[Future[list[dict[str, Any]]]] = []
        self.spill: Path | None = None
        self.spilled = 0

    def _convert(
        self, kind: str, convert: Callable[..., Converted], path: Path, src: Src
    ) -> Converted:
        try:
            if kind in PYMUPDF:
                if self.pdf_pool is not None and (got := self.pdf_pool.convert(convert, path, src)):
                    return got
                with self.pdf_lock:
                    return convert(path, src)
            return convert(path, src)
        except (passwords.Locked, *FATAL):
            raise
        except BaseException as e:  # noqa: BLE001
            return self._rescue(path, src, e)

    def _rescue(self, path: Path, src: Src, error: BaseException) -> Converted:
        """A render of a file its native converter gave up on, or the error again

        Encrypted and DRM-locked files are never rendered, since a render would show only
        the lock. A missing extra stays the answer unless a render read something
        """
        from meltify.converters import fallback, unlock

        if path.suffix.lower() not in fallback.NATIVE or unlock.sealed(path):
            raise error
        if isinstance(error, MissingTool):
            out = fallback.convert(path, src, error.name)
            if out.kind == "unknown":
                raise error
            out.needs.append(error.name)
            return out
        why = f"{path.suffix.lower()[1:]} not read ({error_note(error)})"
        return fallback.convert(path, src, why)

    def _melt(self, path: Path, src: Src, row: dict[str, Any]) -> Converted:
        """Convert through the registry, a decrypted copy standing in for a locked file

        The copy keeps the file's name, so the same converter reads it, while cites and
        outputs stay on the original. It goes once nothing waits to OCR its pages
        """
        from meltify.converters import pick
        from meltify.converters.unlock import unlock

        row["kind"], convert = pick(path)
        opened = unlock(path)
        if opened == path:
            return self._convert(row["kind"], convert, path, src)
        converted = None
        try:
            row["kind"], convert = pick(opened)
            converted = self._convert(row["kind"], convert, opened, src)
            return converted
        finally:
            if converted is None or not any(j.path == opened for j in converted.jobs):
                shutil.rmtree(opened.parent, ignore_errors=True)

    def one(
        self, path: Path, src: Src, name: str | None = None, depth: int = 0
    ) -> list[dict[str, Any]]:
        row = finding(src, kind=None, chars=0, needs=[], hidden=0, out=None)
        try:
            converted = self._melt(path, src, row)
        except passwords.Locked as e:
            return [{**row, "needs": [str(e)]}]
        except MissingTool as e:
            return [{**row, "error": str(e), "hint": e.hint, "needs": [e.name]}]
        except FATAL:
            raise
        except BaseException as e:  # noqa: BLE001
            # One unreadable file shouldn't stop the rest of the folder
            return [{**row, "error": error_note(e, 300)}]
        return self.keep(converted, name or self.names[path], depth, row, src.cite())

    def keep(
        self,
        converted: Converted,
        name: str,
        depth: int,
        row: dict[str, Any],
        origin: str,
    ) -> list[dict[str, Any]]:
        """Write the converted item's markdown, queue its OCR and melt its children"""
        if self.shallow:
            # Embedded pictures are only counted, so their bytes aren't held until the run
            # ends. They're listed the way a deep read without engines lists them
            pictures = [j for j in converted.jobs if j.src.img is not None]
            converted.jobs = [j for j in converted.jobs if j.src.img is None]
            converted.needs = job_needs(pictures) + converted.needs
        converted.jobs = [self._spilled(j) for j in converted.jobs]
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
                / fresh(flat_name(Path(member.replace("/", "__"))), stored, i, fold=True)
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
            rows += self.submit(self.one, saved, child_src, child_out, depth + 1)
        return rows

    def submit(self, fn: Callable[..., list[dict[str, Any]]], *args: Any) -> list[dict[str, Any]]:
        """Rows of `fn` now, or none yet when the run's pool takes it as a task of its own

        Nested items get no worker that waits on them, since a pool whose workers all wait
        on their own children would stop. `drain` collects the rows instead. The task runs
        in a copy of this context, which holds the run's password and switches
        """
        if self.pool is None:
            return fn(*args)
        task = self.pool.submit(contextvars.copy_context().run, fn, *args)
        with self.out_lock:
            self.futures.append(task)
        return []

    def drain(self) -> list[dict[str, Any]]:
        """Rows of every submitted task, nested ones included, once all of them are done"""
        rows: list[dict[str, Any]] = []
        while True:
            with self.out_lock:
                batch, self.futures = self.futures, []
            if not batch:
                return rows
            # A task submits its children before it ends, so they're in the next batch
            for task in batch:
                rows += task.result()

    def _spilled(self, job: RecognizeJob) -> RecognizeJob:
        """The job with big picture bytes moved to a temp file the run removes

        Every picture would otherwise stay in memory until OCR reads it after the last
        file. The file holds the same bytes, so the job keeps its OCR cache key
        """
        if job.data is None or len(job.data) <= SPILL_BYTES:
            return job
        from meltify.converters import run

        with self.out_lock:
            if self.spill is None:
                self.spill = run.workdir("meltify-pictures-")
            self.spilled += 1
            target = self.spill / f"{self.spilled}.bin"
        target.write_bytes(job.data)
        return RecognizeJob(job.kind, job.src, path=target, rect=job.rect)

    def url(self, url: str) -> list[dict[str, Any]]:
        from meltify.commands.media import work_name

        src = Src(url)
        row = finding(src, kind="url", chars=0, needs=[], hidden=0, out=None)
        try:
            fetch = importlib.import_module("meltify.fetch")
            web = importlib.import_module("meltify.converters.web")
        except ImportError:
            return [{**row, "needs": ["url support missing"]}]
        with self.name_lock:
            name = unique_name(work_name(url), self.taken, url)
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
                converted = self._melt(got.path, src, row)
        except passwords.Locked as e:
            return [{**row, "needs": [str(e)]}]
        except MissingTool as e:
            return [{**row, "error": str(e), "hint": e.hint, "needs": [e.name]}]
        except FATAL:
            raise
        except BaseException as e:  # noqa: BLE001
            return [{**row, "error": error_note(e, 300)}]
        origin = " ".join([src.cite(), *(f"{k}={v}" for k, v in meta.items() if v)])
        return self.keep(converted, name, 0, row, origin)

    def _media(self, url: str, src: Src, work: Path) -> Converted:
        from meltify.commands.media import download
        from meltify.converters.subtitle import parse_subtitles
        from meltify.recognize import span

        if self.shallow:
            return Converted("media", needs=["media"])
        video, subs = download(
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
                lines = [f"{span(s, e)}| {text}" for s, e, text in cues]
                out.blocks.append(Block(src, "\n".join(lines)))
                self.transcribed.add(src)
                break
        if video is not None:
            out.jobs.append(RecognizeJob("video", src, path=video))
        return out

    def finish(self, recognize: Callable[..., list[Any]] | None) -> list[dict[str, Any]]:
        """Fold recognized text into each item, write its markdown and return disputed rows"""
        # Workers finish in any order, while OCR's budget goes to jobs in the order given
        self.outputs.sort(key=lambda o: o.row["cite"])
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
            o.converted.write(o.target, o.origin)
        return disputed


def run(
    args: argparse.Namespace, settings: dict[str, Any], password: str | None = None
) -> Envelope:
    """Melt the inputs `args` names, with `password` for encrypted ones when given in code

    Converters read the run's password and switches from a context variable, and a private
    copy of the context keeps them from reaching concurrent or later runs
    """
    return contextvars.copy_context().run(_run, args, settings, password)


def _run(args: argparse.Namespace, settings: dict[str, Any], password: str | None) -> Envelope:
    from meltify.recognize import Options, Recognizer

    env = Envelope(command=NAME, version=__version__)
    conf = settings.get("read", {})
    ocr_conf = settings.get("ocr", {})
    media_conf = settings.get("media", {})
    out_dir = Path(settings["out_dir"]) / "read"
    urls = [p for p in args.paths if is_url(p)]
    local = [Path(p) for p in args.paths if not is_url(p)]
    files = list(iter_files(local))
    from meltify.converters.run import RunContext, Temps, use

    # Renders, decrypted copies and spilled members stay until OCR has read them, then go
    # with the run instead of waiting for the process to exit
    temps = Temps()
    looks = bool(settings.get("render", {}).get("quicklook", True))
    use(
        RunContext(
            password=given_password(args, env.warnings.append, password),
            fallback=bool(conf.get("fallback", True)),
            shallow=args.shallow,
            parquet_rows=int(conf.get("parquet_rows", 200)),
            quicklook=looks,
            temps=temps,
        )
    )
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

    rows: list[dict[str, Any]] = []
    jobs = max(1, args.jobs or int(conf.get("jobs", 8)))
    try:
        with ThreadPoolExecutor(jobs) as pool, PdfPool.start(jobs) as pdf_pool:
            reader.pool, reader.pdf_pool = pool, pdf_pool
            # Worker threads start in an empty context, so each task runs in a copy of this one
            for f in files:
                reader.submit(reader.one, f, Src(str(f)))
            for u in urls:
                reader.submit(reader.url, u)
            rows += reader.drain()
        reader.pool = reader.pdf_pool = None

        recognizer = None
        if not args.shallow:
            opts = Options(
                engines=args.engines or str(ocr_conf.get("engines", "auto")),
                asr=args.asr or str(settings.get("asr", {}).get("engine", "auto")),
                lang=str(settings.get("lang", "ko")),
                asr_lang=str(settings.get("asr", {}).get("lang", "auto")),
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
    finally:
        temps.remove()
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
