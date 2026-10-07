from __future__ import annotations

import argparse
import contextvars
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any

from meltify import __version__, lang
from meltify.config import pick
from meltify.converters import SOURCE_MARK
from meltify.evidence import Envelope, Src
from meltify.files import common_root, flat_names, iter_files
from meltify.melt import Reader
from meltify.output import append_jsonl
from meltify.passwords import given_password
from meltify.pdfpool import PdfPool

if TYPE_CHECKING:
    from meltify.recognize import Recognizer

NAME = "read"
HELP = (
    "melt files, folders and URLs of any format into cited markdown,"
    " with images and recordings read by local OCR and speech engines"
)
COLUMNS = ["kind", "chars", "needs", "out", "cite"]


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
    add(
        p,
        "--lang",
        type=lang.code,
        help="text language for OCR, like ko, en, ja, zh, de, fr or es (default: lang, ko)",
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


# Longest first line read back when a rerun sorts out which files are stale
MAX_CITE = 8192
ORIGIN_META = re.compile(r"( (?:fetched_at|final_url|sha256|etag)=\S*)+$")


def is_url(raw: str) -> bool:
    return raw.lower().startswith(("http://", "https://"))


def drop_stale(out_dir: Path, rows: list[dict[str, Any]]) -> None:
    """Remove the markdown earlier runs left in `out_dir` that this run didn't write

    The index only lists the latest run, so leftovers beside it would turn up in a grep of
    the folder as if they belonged to it. Only files meltify wrote go, known by their first
    line. An item that failed this run keeps whatever it and its members wrote before, and a
    run that found no input at all leaves the folder alone
    """
    if not rows or not out_dir.is_dir():
        return
    written = {Path(r["out"]).name for r in rows if r.get("out")}
    failed = [r["cite"] for r in rows if not r.get("out")]
    for md in out_dir.glob("*.md"):
        if md.name in written or (cite := _source(md)) is None:
            continue
        if any(cite == f or cite.startswith(f + "#") for f in failed):
            continue
        md.unlink(missing_ok=True)
        shutil.rmtree(out_dir / "attachments" / md.stem, ignore_errors=True)


def _source(path: Path) -> str | None:
    """The cite in a melted file's first line, or None for a file meltify didn't write"""
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            first = f.readline(MAX_CITE)
    except OSError:
        return None
    if not first.startswith(SOURCE_MARK):
        return None
    origin = first[len(SOURCE_MARK) :].rstrip().removesuffix("-->").rstrip()
    # URL items append their fetch metadata after the cite
    return ORIGIN_META.sub("", origin)


def run(
    args: argparse.Namespace, settings: dict[str, Any], password: str | None = None
) -> Envelope:
    """Melt the inputs `args` names, with `password` for encrypted ones when given in code

    Converters read the run's password and switches from a context variable, and a private
    copy of the context keeps them from reaching concurrent or later runs
    """
    return contextvars.copy_context().run(_run, args, settings, password)


def _run(args: argparse.Namespace, settings: dict[str, Any], password: str | None) -> Envelope:
    from meltify.converters.run import RunContext, Temps, use

    env = Envelope(command=NAME, version=__version__)
    out_dir = Path(settings["out_dir"]) / "read"
    urls = [p for p in args.paths if is_url(p)]
    local = [Path(p) for p in args.paths if not is_url(p)]
    files = list(iter_files(local))
    # Renders, decrypted copies and spilled members stay until OCR has read them, then go
    # with the run instead of waiting for the process to exit
    temps = Temps()
    use(
        RunContext.from_settings(
            settings,
            password=given_password(args, env.warnings.append, password),
            shallow=args.shallow,
            temps=temps,
        )
    )
    reader = _reader(args, settings, out_dir, flat_names(files, common_root(local)))
    env.inputs = [{"path": str(p)} for p in args.paths]
    # 0 and below run one file at a time, like 1
    jobs = max(1, int(pick(args.jobs, settings["read"]["jobs"])))
    recognizer = _recognizer(args, settings, env)
    if recognizer is not None:
        recognizer.check_named(ocr=args.engines is not None, asr=args.asr is not None)
    try:
        rows = _convert_all(reader, files, urls, jobs)
        started = time.monotonic()
        rows += reader.finish(recognizer.run if recognizer else None)
    finally:
        temps.remove()
    rows.sort(key=lambda r: r["cite"])
    _write_index(env, out_dir / "index.jsonl", rows)
    _summarize(env, out_dir, rows, recognizer, started)
    return env


def _reader(
    args: argparse.Namespace, settings: dict[str, Any], out_dir: Path, names: dict[Path, str]
) -> Reader:
    return Reader(
        out_dir,
        names,
        shallow=args.shallow,
        fetch_opts={
            "max_bytes": int(pick(args.max_bytes, settings["read"]["max_bytes"])),
            "allow_private": args.allow_private,
            "ignore_robots": args.ignore_robots,
            "refresh": args.refresh,
            "render": args.render,
        },
        whole=args.whole,
        media_conf=settings["media"],
    )


def _convert_all(
    reader: Reader, files: list[Path], urls: list[str], jobs: int
) -> list[dict[str, Any]]:
    """Rows of every file and URL, nested items included, converted `jobs` at a time"""
    with ThreadPoolExecutor(jobs) as pool, PdfPool.start(jobs) as pdf_pool:
        reader.pool, reader.pdf_pool = pool, pdf_pool
        # Worker threads start in an empty context, so each task runs in a copy of this one
        for f in files:
            reader.submit(reader.one, f, Src(str(f)))
        for u in urls:
            reader.submit(reader.url, u)
        rows = reader.drain()
    reader.pool = reader.pdf_pool = None
    return rows


def _recognizer(
    args: argparse.Namespace, settings: dict[str, Any], env: Envelope
) -> Recognizer | None:
    from meltify.recognize import Options, Recognizer

    if args.shallow:
        return None
    opts = Options.from_settings(
        settings,
        engines=args.engines,
        asr=args.asr,
        budget=args.budget,
        frames=args.frames,
        refresh=args.refresh,
    )
    return Recognizer(opts, settings, env.warnings.append)


def _write_index(env: Envelope, index: Path, rows: list[dict[str, Any]]) -> None:
    drop_stale(index.parent, rows)
    index.unlink(missing_ok=True)
    append_jsonl(index, rows)
    env.results = rows
    env.artifact(str(index), "results")


def _summarize(
    env: Envelope,
    out_dir: Path,
    rows: list[dict[str, Any]],
    recognizer: Recognizer | None,
    started: float,
) -> None:
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
