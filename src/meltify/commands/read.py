from __future__ import annotations

import argparse
import contextvars
import re
import shutil
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any

from meltify import __version__, lang
from meltify.config import pick
from meltify.converters import SOURCE_MARK
from meltify.evidence import Envelope, Src
from meltify.files import Pages, common_root, flat_names, iter_files
from meltify.melt import DETAIL, Reader
from meltify.needs import count
from meltify.output import append_jsonl
from meltify.passwords import given_password
from meltify.pdfpool import PdfPool

if TYPE_CHECKING:
    from meltify.engines.ocr import Reading
    from meltify.ffmpeg import Window
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
        "--no-pictures",
        action="store_true",
        help="don't save a small WebP of each picture beside the markdown",
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
        help="frames to keep and OCR per video, spread over it, 0 for none"
        " (default: read.frames, 20)",
    )
    add(
        p,
        "--engines",
        help="OCR engines like vision,gemini or an endpoint you named (default: auto, local "
        "engines and endpoints only)",
    )
    add(
        p,
        "--lang",
        type=lang.code,
        help="text language for OCR, like ko, en, ja, zh, de, fr or es (default: lang, ko)",
    )
    add(
        p,
        "--asr",
        help="whispercpp, api or an endpoint you named (default: asr.engine, local only)",
    )
    add(p, "--refresh", action="store_true", help="ignore cached OCR, speech and fetches")
    add(p, "--pages", type=_pages, help="PDF pages like 1,3-5 (default: all)")
    ocr = p.add_argument_group("OCR")
    add(
        ocr,
        "--upscale",
        type=_positive,
        help="resize factor for pictures before OCR (default: ocr.upscale, auto per picture)",
    )
    add(
        ocr,
        "--ocr-pages",
        action="store_true",
        help="OCR every PDF page as drawn, even one with a text layer, to check that layer",
    )
    add(ocr, "--no-sharpen", action="store_true", help="skip the sharpen filter")
    add(ocr, "--equalize", action="store_true", help="stretch contrast before OCR")
    add(
        ocr,
        "--compare",
        choices=["numbers", "tokens"],
        default="numbers",
        help="what to compare across engines (default: numbers)",
    )
    add(
        ocr,
        "--reading",
        action="append",
        default=[],
        metavar="NAME=FILE",
        help="lines read elsewhere, say by an agent viewing the one image given,"
        " compared as one more engine",
    )
    hidden = p.add_argument_group("hidden text in PDFs")
    add(hidden, "--hidden", action="store_true", help="list every hidden span as a row")
    add(
        hidden,
        "--contrast",
        action="store_true",
        help="also save each page rendered in stretched contrast, to expose text drawn in images",
    )
    media = p.add_argument_group("recordings and videos")
    add(media, "--start", type=float, metavar="SEC", help="where to start, in seconds")
    add(media, "--end", type=float, metavar="SEC", help="where to stop, in seconds from the start")
    add(
        media,
        "--scene",
        type=_fraction,
        help="scene-change threshold, 0 to 1 (default: media.scene, 0.3)",
    )
    add(
        media,
        "--fps",
        type=_positive,
        help="also take interval frames at this rate (default: media.fps, none)",
    )
    add(
        media,
        "--keep-duplicates",
        action="store_true",
        help="keep near-identical frames, marked with the time they repeat",
    )
    add(
        media,
        "--subs-only",
        action="store_true",
        help="take transcripts from subtitles alone, skipping speech engines and frames",
    )
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
    add(
        web,
        "--max-bytes",
        type=int,
        help="largest download in bytes (default: read.max_bytes, 20000000)",
    )
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
ORIGIN_META = re.compile(r"( (?:fetched_at|final_url|sha256|etag|url)=\S*)+$")
ORIGIN_URL = re.compile(r" url=(\S+)$")


def _positive(raw: str) -> float:
    n = float(raw)
    if not n > 0:
        raise argparse.ArgumentTypeError(f"must be above 0, got {raw}")
    return n


def _fraction(raw: str) -> float:
    n = float(raw)
    if not 0 <= n <= 1:
        raise argparse.ArgumentTypeError(f"must be between 0 and 1, got {raw}")
    return n


def _pages(raw: str) -> Pages:
    try:
        return Pages.parse(raw)
    except ValueError as e:
        # argparse shows its own words for a ValueError, and these say what to fix
        raise argparse.ArgumentTypeError(str(e)) from None


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
        if md.name in written or not (sources := _sources(md)):
            continue
        if any(s == f or s.startswith(f + "#") for s in sources for f in failed):
            continue
        md.unlink(missing_ok=True)
        shutil.rmtree(out_dir / "attachments" / md.stem, ignore_errors=True)


def _sources(path: Path) -> list[str]:
    """The cite in a melted file's first line, and the URL as given for one fetched from
    the web, or nothing for a file meltify didn't write
    """
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            first = f.readline(MAX_CITE)
    except OSError:
        return []
    if not first.startswith(SOURCE_MARK):
        return []
    origin = first[len(SOURCE_MARK) :].rstrip().removesuffix("-->").rstrip()
    # URL items append their fetch metadata after the cite, where it may have redirected
    given = ORIGIN_URL.search(origin)
    return [ORIGIN_META.sub("", origin), *([given[1]] if given else [])]


def run(
    args: argparse.Namespace, settings: dict[str, Any], password: str | None = None
) -> Envelope:
    """Melt the inputs `args` names, with `password` for encrypted ones when given in code

    Converters read the run's password and switches from a context variable, and a private
    copy of the context keeps them from reaching concurrent or later runs
    """
    return contextvars.copy_context().run(_run, args, settings, password)


def _run(args: argparse.Namespace, settings: dict[str, Any], password: str | None) -> Envelope:
    from meltify.converters.run import PdfLook, RunContext, Temps, use
    from meltify.ffmpeg import Window

    env = Envelope(command=NAME, version=__version__)
    out_dir = Path(settings["out_dir"]) / "read"
    urls = [p for p in args.paths if is_url(p)]
    local = [Path(p) for p in args.paths if not is_url(p)]
    _check(args, urls)
    files = list(iter_files(local))
    window = Window(args.start, args.end)
    # Renders, decrypted copies and spilled members stay until OCR has read them, then go
    # with the run instead of waiting for the process to exit
    temps = Temps()
    use(
        RunContext.from_settings(
            settings,
            password=given_password(args, env.warnings.append, password),
            shallow=args.shallow,
            temps=temps,
            pdf=PdfLook(
                pages=args.pages,
                ocr_pages=args.ocr_pages,
                hidden=args.hidden,
                contrast_dpi=int(settings["ocr"]["dpi"]) if args.contrast else None,
            ),
            window=window,
            subs_only=args.subs_only,
        )
    )
    reader = _reader(args, settings, out_dir, flat_names(files, common_root(local)))
    env.inputs = [{"path": str(p)} for p in args.paths]
    # 0 and below run one file at a time, like 1
    jobs = max(1, int(pick(args.jobs, settings["read"]["jobs"])))
    recognizer = _recognizer(args, settings, env, window)
    if recognizer is not None:
        recognizer.check_named(ocr=args.engines is not None, asr=args.asr is not None)
    try:
        rows = _convert_all(reader, files, urls, jobs)
        if args.reading and (n := reader.pictures()) != 1:
            # A reading describes one picture, so it can't be compared with none or several
            raise ValueError(
                f"--reading needs exactly one image or OCR'd PDF page, got {n}."
                " Add --ocr-pages for a page with a text layer"
            )
        started = time.monotonic()
        rows += reader.finish(recognizer.run if recognizer else None)
    finally:
        temps.remove()
    rows.sort(key=lambda r: r["cite"])
    _write_index(env, out_dir / "index.jsonl", rows)
    _summarize(env, out_dir, rows, recognizer, started, reader.unchecked)
    if args.hidden:
        _hidden_notes(env, rows, args.contrast, reader.hidden_unchecked)
    return env


def _hidden_notes(
    env: Envelope, rows: list[dict[str, Any]], contrast: bool, unchecked: int
) -> None:
    """Say what --hidden left unchecked, so no hidden span found never reads as all clear"""
    if not any(r["kind"] == "hidden" for r in rows):
        env.summary += ", 0 hidden spans"
    if unchecked:
        env.warnings.append(
            "--hidden checks PDFs and SVGs only,"
            f" so {count(unchecked, 'other item')} went unchecked"
        )
    if not contrast and any(r["kind"] == "pdf" for r in rows):
        env.warnings.append(
            "text drawn inside or over images isn't covered, add --contrast to render it"
        )


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
        webp=_webp(args, settings["read"]),
        # A failed --reading check comes after conversion, so nothing is written before it
        defer=bool(args.reading),
    )


def _webp(args: argparse.Namespace, conf: dict[str, Any]) -> tuple[int, int] | None:
    """Longest side and WebP quality of saved pictures, or None when they're off"""
    side, quality = int(conf["picture_side"]), int(conf["picture_quality"])
    if not 1 <= quality <= 100:
        raise ValueError(f"read.picture_quality must be 1 to 100, not {quality}")
    if args.shallow or args.no_pictures or side <= 0:
        return None
    return side, quality


def _check(args: argparse.Namespace, urls: list[str]) -> None:
    """Refuse flags that don't make sense together, which no single flag's type can see"""
    if args.start is not None and args.end is not None and args.start >= args.end:
        raise ValueError(f"--start {args.start:g} must come before --end {args.end:g}")
    if args.reading and (len(args.paths) != 1 or urls):
        # A reading describes one picture, so it can't stand beside several inputs
        raise ValueError("--reading needs exactly one local image or PDF page")
    if args.reading and args.shallow:
        raise ValueError("--reading needs OCR, which --shallow skips")


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
    args: argparse.Namespace, settings: dict[str, Any], env: Envelope, window: Window
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
        upscale=args.upscale,
        no_sharpen=args.no_sharpen,
        equalize=args.equalize,
        compare=args.compare,
        readings=tuple(_reading(item) for item in args.reading),
        scene=args.scene,
        fps=args.fps,
        keep_duplicates=args.keep_duplicates,
        window=window,
    )
    return Recognizer(opts, settings, env.warnings.append)


def _reading(item: str) -> Reading:
    """A `--reading NAME=FILE`, one line of text per line of the file"""
    from meltify.engines.ocr import Reading

    name, eq, file = item.partition("=")
    if not (name and eq and file):
        raise ValueError(f"--reading expects NAME=FILE, got {item!r}")
    lines = Path(file).read_text("utf-8").splitlines()
    return Reading(name, [x for x in lines if x.strip()])


def _write_index(env: Envelope, index: Path, rows: list[dict[str, Any]]) -> None:
    drop_stale(index.parent, rows)
    index.unlink(missing_ok=True)
    append_jsonl(index, rows)
    env.results = rows
    env.artifact(str(index), "results")
    added = {c for r in rows if r["kind"] in DETAIL for c in DETAIL[r["kind"]][1]}
    if added:
        extra = [c for c in ("text", "reasons", "path") if c in added]
        env.columns = [*COLUMNS[:-1], *extra, COLUMNS[-1]]


def _summarize(
    env: Envelope,
    out_dir: Path,
    rows: list[dict[str, Any]],
    recognizer: Recognizer | None,
    started: float,
    unchecked: int,
) -> None:
    errors = [r for r in rows if r.get("error")]
    pending = [r for r in rows if r["needs"]]
    counts = Counter(r["kind"] for r in rows if r["kind"] in DETAIL)
    disputed = counts["disputed"]
    env.summary = (
        f"{len(rows) - counts.total()} items melted into {out_dir}, "
        f"{len(errors)} errors, {len(pending)} need more work"
    )
    # Disputed values have their own clause with the recognition numbers below
    for kind, (label, _) in DETAIL.items():
        if counts[kind] and kind != "disputed":
            env.summary += f", {counts[kind]} {label}"
    if recognizer is not None and recognizer.seen:
        env.summary += (
            f", {recognizer.seen} images and recordings read in"
            f" {time.monotonic() - started:.1f}s ({recognizer.cached} cached),"
            f" {disputed} disputed values"
        )
        if unchecked:
            env.summary += f", {unchecked} pictures read by one engine only"
            env.warnings.append(
                f"{unchecked} pictures were read by one engine only, so nothing there was"
                " cross-checked. Add an engine or --reading"
            )
    for r in errors:
        env.warnings.append(
            f"{r['cite']}: {r['error']}" + (f" ({r['hint']})" if r.get("hint") else "")
        )
