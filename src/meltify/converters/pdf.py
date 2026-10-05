from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from meltify.converters import Block, Converted
from meltify.evidence import Src

# A page with less text than this is probably a scan
SCAN_CHARS = 20


def convert(path: Path, src: Src) -> Converted:
    import pymupdf

    from meltify.forensics.pdf import scan

    out = Converted("pdf")
    scanned = []
    with pymupdf.open(path) as doc:
        for i, page in enumerate(doc, start=1):
            text = page.get_text("text").strip()
            if len(text) < SCAN_CHARS:
                scanned.append(i)
            if text:
                out.blocks.append(Block(replace(src, page=i), text))
    if scanned:
        out.needs.append("ocr pages " + ",".join(map(str, scanned)))
    out.hidden = len(scan(str(path)))
    if out.hidden:
        out.needs.append(f"hidden {out.hidden} spans")
    return out
