from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meltify import __version__, lang
from meltify.config import pick
from meltify.engines import ocr as engines
from meltify.engines.consensus import EngineReading, compare, disputed_row
from meltify.evidence import Envelope, Src, finding
from meltify.files import flat_name, parse_pages
from meltify.safe import MissingTool, attempt

NAME = "ocr"
HELP = "read text in images or scanned PDF pages with several engines and show where they disagree"
COLUMNS = ["type", "engine", "text", "cite"]


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("inputs", nargs="+", type=Path, help="images or PDFs")
    p.add_argument("--pages", help="PDF pages like 1,3-5 (default: all)")
    p.add_argument(
        "--engines",
        help=f"comma-separated list of {', '.join(engines.ENGINES)}"
        " (default: auto, local engines only)",
    )
    p.add_argument(
        "--lang",
        type=lang.code,
        help="text language for OCR, like ko, en, ja, zh, de, fr or es (default: lang, ko)",
    )
    p.add_argument(
        "--upscale", type=float, help="resize factor for images before OCR (PDF pages use dpi)"
    )
    p.add_argument("--no-sharpen", action="store_true", help="skip the sharpen filter")
    p.add_argument("--equalize", action="store_true", help="stretch contrast before OCR")
    p.add_argument(
        "--reading",
        action="append",
        default=[],
        metavar="NAME=FILE",
        help="lines read elsewhere, say by an agent viewing the image, compared as one more engine",
    )
    p.add_argument(
        "--compare",
        choices=["numbers", "tokens"],
        default="numbers",
        help="what to compare across engines",
    )


@dataclass(frozen=True)
class Target:
    """One image to read and how its pixels map back to the original input"""

    image: Any
    path: str
    page: int | None
    upscale: float
    to_original: float  # multiply prepared px by this to get original px or PDF pt
    unit: str

    def src(self, bbox: tuple[float, float, float, float] | None) -> Src:
        scaled = None if bbox is None else tuple(round(v * self.to_original, 1) for v in bbox)
        return Src(self.path, page=self.page, bbox=scaled, unit=self.unit if scaled else None)


def _targets(path: Path, pages_spec: str | None, dpi: int, factor: float) -> list[Target]:
    from PIL import Image, UnidentifiedImageError

    from meltify import imaging

    if path.suffix.lower() != ".pdf":
        try:
            image = Image.open(path).convert("RGB")
        except UnidentifiedImageError:
            raise ValueError(f"{path} isn't an image or a PDF, run meltify read on it") from None
        return [Target(image, str(path), None, factor, 1 / factor, "px")]
    import pymupdf

    with pymupdf.open(path) as doc:
        pages = parse_pages(pages_spec, doc.page_count)
    # The render dpi already sets the resolution, so upscaling again just multiplies pixels
    return [
        Target(imaging.render_pdf_page(str(path), n, dpi), str(path), n, 1.0, 72 / dpi, "pt")
        for n in pages
    ]


def _count(path: Path, pages_spec: str | None) -> int:
    if path.suffix.lower() != ".pdf":
        return 1
    import pymupdf

    with pymupdf.open(path) as doc:
        return len(parse_pages(pages_spec, doc.page_count))


def run(args: argparse.Namespace, settings: dict[str, Any]) -> Envelope:
    from meltify import imaging

    env = Envelope(command=NAME, version=__version__)
    conf = settings["ocr"]
    factor = float(pick(args.upscale, conf["upscale"]))
    if factor <= 0:
        raise ValueError(f"--upscale must be above 0, got {factor:g}")
    sharpen = conf["sharpen"] and not args.no_sharpen
    out_dir = Path(settings["out_dir"]) / "ocr"

    selected = engines.ready(pick(args.engines, conf["engines"]), settings)
    given = _given_readings(args.reading)
    if not selected and not given:
        raise MissingTool(
            "ocr engine",
            f"{engines.INSTALL_HINT}. You can also pass --engines gemini with GEMINI_API_KEY,"
            " or --reading NAME=FILE",
        )
    if given and (n := sum(_count(p, args.pages) for p in args.inputs)) != 1:
        # A reading describes one image, so comparing it with other pages would invent disputes
        raise ValueError(f"--reading needs exactly one image or page, got {n}")

    for path in args.inputs:
        env.inputs.append({"path": str(path)})
        for t in _targets(path, args.pages, int(conf["dpi"]), factor):
            prepared = imaging.upscale(t.image, t.upscale, sharpen)
            if args.equalize:
                prepared = imaging.equalize(prepared)
            work = out_dir / (flat_name(path) + (f".p{t.page}" if t.page else ""))
            prep_path = imaging.save(prepared, work / "prepared.png")
            env.artifact(str(prep_path), "prepared")
            readers = [*selected, *given]
            readings = _read(env, t, readers, prepared, prep_path, work, int(conf["tile_max"]))
            _cross_check(env, t, readings, args.compare)
    env.summary = env.summary.strip()
    return env


def _given_readings(items: list[str]) -> list[engines.Reading]:
    """Readings from `--reading NAME=FILE`, one line of text per line of the file"""
    readings = []
    for item in items:
        name, eq, file = item.partition("=")
        if not (name and eq and file):
            raise ValueError(f"--reading expects NAME=FILE, got {item!r}")
        lines = Path(file).read_text("utf-8").splitlines()
        readings.append(engines.Reading(name, [x for x in lines if x.strip()]))
    return readings


def _read(
    env: Envelope,
    t: Target,
    readers: list[engines.Engine | engines.Reading],
    prepared: Any,
    prep_path: Path,
    work: Path,
    tile_max: int,
) -> list[EngineReading]:
    """Every reader's lines for one target, each added to the results as it comes in"""
    where = t.src(None).cite()
    size = (prepared.width, prepared.height)
    readings: list[EngineReading] = []
    for e in readers:
        if isinstance(e, engines.Remote):
            got = attempt(engines.read_tiles, e, prepared, tile_max, work)
        else:
            got = attempt(e.recognize, prep_path, size)
        if not got.ok:
            # If one engine fails, the others still read and compare
            env.warnings.append(f"{e.name} failed on {where}: {got.error}")
            continue
        boxes = got.value
        readings.append(EngineReading(e.name, e.kind, boxes))
        for b in boxes:
            env.results.append(
                finding(t.src(b.bbox), type="line", engine=e.name, text=b.text, conf=b.conf)
            )
    return readings


def _cross_check(env: Envelope, t: Target, readings: list[EngineReading], mode: str) -> None:
    where = t.src(None).cite()
    if len(readings) < 2:
        # A single reading has nothing to dispute, so its lines stand alone
        env.warnings.append(f"only one engine read {where}, nothing was cross-checked")
        env.summary += f"{where}: unchecked, add an engine or --reading. "
        return
    verdicts = compare(readings, mode)
    disputed = [v for v in verdicts if not v.agreed]
    for v in disputed:
        env.results.insert(0, disputed_row(t.src(v.bbox), v, engine="all"))
    agreed = sum(v.agreed for v in verdicts)
    env.summary += f"{where}: {agreed} agreed, {len(disputed)} disputed {mode}. "
