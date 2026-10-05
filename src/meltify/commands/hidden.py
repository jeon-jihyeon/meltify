from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from meltify import __version__
from meltify.evidence import USAGE, Envelope, Src, finding
from meltify.files import flat_names, parse_pages

NAME = "hidden"
HELP = "find text a PDF carries but a reader doesn't see, with page and position"
COLUMNS = ["text", "reasons", "cite"]


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("pdf", nargs="+", type=Path, help="PDF files")
    p.add_argument("--pages", help="pages like 1,3-5 (default: all)")
    p.add_argument(
        "--contrast",
        action="store_true",
        help="also render each page with stretched contrast to expose text drawn inside images",
    )


def run(args: argparse.Namespace, settings: dict[str, Any]) -> Envelope:
    import pymupdf

    from meltify import imaging
    from meltify.forensics.pdf import scan

    env = Envelope(command=NAME, version=__version__)
    out_dir = Path(settings["out_dir"]) / "hidden"
    dpi = int(settings.get("ocr", {}).get("dpi", 300))
    names = flat_names(args.pdf)
    for path in args.pdf:
        with pymupdf.open(path) as doc:
            pages = parse_pages(args.pages, doc.page_count)
        if not pages:
            env.error(USAGE, f"{path} has no pages to scan")
            return env
        env.inputs.append({"path": str(path), "pages": len(pages)})
        for span in scan(str(path), pages):
            src = Src(str(path), page=span.page, bbox=span.bbox, unit="pt")
            env.results.append(
                finding(
                    src,
                    text=span.text,
                    reasons=list(span.reasons),
                    color=span.color,
                    background=span.background,
                    size=span.size,
                    opacity=span.opacity,
                )
            )
        if args.contrast:
            for n in pages:
                image = imaging.equalize(imaging.render_pdf_page(str(path), n, dpi))
                target = out_dir / names[path] / f"p{n}.contrast.png"
                env.artifact(str(imaging.save(image, target)), f"contrast page {n}")

    env.summary = f"{len(env.results)} hidden spans in {len(args.pdf)} files"
    if args.contrast:
        env.summary += (
            ". To catch text inside images, read the contrast images or run meltify ocr on them"
        )
    else:
        env.warnings.append(
            "text drawn inside or over images isn't covered, add --contrast to render it"
        )
    return env
