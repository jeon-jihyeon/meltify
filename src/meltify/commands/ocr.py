from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meltify import __version__
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
        help="comma-separated list of vision, paddle, claude, gemini, openai"
        " (default: auto, local engines only)",
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
    from PIL import Image

    from meltify import imaging

    if path.suffix.lower() != ".pdf":
        return [Target(Image.open(path).convert("RGB"), str(path), None, factor, 1 / factor, "px")]
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


def _overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _read_tiles(e: Any, prepared: Any, tile_max: int, folder: Path) -> list[Any]:
    from meltify import imaging
    from meltify.engines import ocr as engines

    boxes = []
    seen: list[tuple[tuple[int, int, int, int], Counter[str]]] = []
    # Vision APIs shrink large images, so keep each tile under their limit
    for i, tile in enumerate(imaging.tiles(prepared, tile_max)):
        tile_path = imaging.save(tile.image, folder / f"tile{i}.png")
        rect = (tile.x, tile.y, tile.x + tile.image.width, tile.y + tile.image.height)
        lines = [b.text for b in e.recognize(tile_path, tile.image.size)]
        # Remote lines carry no position, so a line in the overlap comes back from both tiles.
        # Drop one copy for each line an overlapping tile already read
        before: Counter[str] = Counter()
        for r, c in seen:
            if _overlap(r, rect):
                before |= c
        seen.append((rect, Counter(lines)))
        for text in lines:
            if before[text]:
                before[text] -= 1
                continue
            boxes.append(engines.TextBox(text, rect))
    return boxes


def run(args: argparse.Namespace, settings: dict[str, Any]) -> Envelope:
    from meltify import imaging
    from meltify.engines import ocr as engines
    from meltify.engines.consensus import EngineReading, compare

    env = Envelope(command=NAME, version=__version__)
    conf = settings.get("ocr", {})
    factor = args.upscale or float(conf.get("upscale", 3))
    sharpen = conf.get("sharpen", True) and not args.no_sharpen
    tile_max = int(conf.get("tile_max", 2576))
    out_dir = Path(settings["out_dir"]) / "ocr"

    selected = engines.select(args.engines or conf.get("engines", "auto"), settings)
    for e in selected:
        if (why := e.missing()) is not None:
            raise MissingTool(e.name, why)
    readings_in = []
    for item in args.reading:
        name, eq, file = item.partition("=")
        if not (name and eq and file):
            raise ValueError(f"--reading expects NAME=FILE, got {item!r}")
        lines = Path(file).read_text("utf-8").splitlines()
        readings_in.append(engines.Reading(name, [x for x in lines if x.strip()]))
    if not selected and not readings_in:
        raise MissingTool(
            "ocr engine",
            "pip install ocrmac on macOS, or meltify doctor --install ocr-paddle elsewhere. "
            "You can also pass --engines gemini with GEMINI_API_KEY, or --reading NAME=FILE",
        )

    if readings_in and (n := sum(_count(p, args.pages) for p in args.inputs)) != 1:
        # A reading describes one image, so comparing it with other pages would invent disputes
        raise ValueError(f"--reading needs exactly one image or page, got {n}")

    for path in args.inputs:
        env.inputs.append({"path": str(path)})
        for t in _targets(path, args.pages, int(conf.get("dpi", 300)), factor):
            prepared = imaging.upscale(t.image, t.upscale, sharpen)
            if args.equalize:
                prepared = imaging.equalize(prepared)
            stem = flat_name(path) + (f".p{t.page}" if t.page else "")
            prep_path = imaging.save(prepared, out_dir / stem / "prepared.png")
            env.artifact(str(prep_path), "prepared")
            size = (prepared.width, prepared.height)

            where = t.src(None).cite()
            readings: list[EngineReading] = []
            for e in [*selected, *readings_in]:
                if isinstance(e, engines.Remote):
                    got = attempt(_read_tiles, e, prepared, tile_max, out_dir / stem)
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

            if len(readings) < 2:
                # A single reading has nothing to dispute, so its lines stand alone
                env.warnings.append(f"only one engine read {where}, nothing was cross-checked")
                env.summary += f"{where}: unchecked, add an engine or --reading. "
                continue
            verdicts = compare(readings, args.compare)
            disputed = [v for v in verdicts if not v.agreed]
            for v in disputed:
                counts = ", ".join(f"{n} {k}" for n, k in v.counts.items())
                env.results.insert(
                    0,
                    finding(
                        t.src(v.bbox),
                        type="disputed",
                        engine="all",
                        text=f"{v.value}  ({counts})",
                        value=v.value,
                        counts=v.counts,
                    ),
                )
            agreed = sum(v.agreed for v in verdicts)
            env.summary += f"{where}: {agreed} agreed, {len(disputed)} disputed {args.compare}. "
    env.summary = env.summary.strip()
    return env
