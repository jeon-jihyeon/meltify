from __future__ import annotations

from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any

from meltify.converters import Block, Converted, RecognizeJob
from meltify.evidence import Src

# A page with less text than this is probably a scan
SCAN_CHARS = 20
# Drawn smaller than about 50 by 50 pt, an image is a bullet, an icon or a stamp
MIN_AREA_PT = 2500
# In a 3+ page document, an image on more than half the pages is a logo or a letterhead
REPEAT_SHARE = 0.5
# An image this much of a page with real text under it is a searchable scan, already read
COVER_SHARE = 0.8
# Pillow reads these straight from the PDF stream. Anything else goes through a pixmap
DIRECT = {"png", "jpeg", "jpg"}


def _image_bytes(doc: Any, xref: int) -> bytes:
    import pymupdf

    found = doc.extract_image(xref)
    if found and found.get("ext") in DIRECT and not found.get("smask"):
        return found["image"]
    pix = pymupdf.Pixmap(doc, xref)
    if found and found.get("smask"):
        pix = pymupdf.Pixmap(pix, pymupdf.Pixmap(doc, found["smask"]))
    if pix.colorspace is not None and pix.colorspace.n > 3:
        # PNG has no CMYK, so convert before encoding
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    return pix.tobytes("png")


def _inline_bytes(page: Any, rect: Any) -> bytes:
    # Inline images have no xref to extract, so render just their area instead
    return page.get_pixmap(clip=rect, dpi=200).tobytes("png")


def convert(path: Path, src: Src) -> Converted:
    import pymupdf

    from meltify import imaging
    from meltify.forensics.pdf import scan

    out = Converted("pdf")
    with pymupdf.open(path) as doc:
        drawn = [page.get_image_info(xrefs=True) for page in doc]
        pages_with: Counter[int] = Counter()
        for infos in drawn:
            pages_with.update({i["xref"] for i in infos if i["xref"]})
        repeated = {
            x
            for x, n in pages_with.items()
            if doc.page_count >= 3 and n / doc.page_count > REPEAT_SHARE
        }
        for i, page in enumerate(doc, start=1):
            text = page.get_text("text").strip()
            if text:
                out.blocks.append(Block(replace(src, page=i), text))
            if len(text) < SCAN_CHARS:
                # A full-page image with no text is a scan. OCR the rendered page instead of
                # its images, so text drawn on top of them is read too
                out.jobs.append(RecognizeJob("page", replace(src, page=i), path=path))
                continue
            for n, info in enumerate(drawn[i - 1], start=1):
                xref = info["xref"]
                bbox = pymupdf.Rect(info["bbox"])
                shown = bbox & page.rect
                if (
                    shown.is_empty
                    or shown.width * shown.height < MIN_AREA_PT
                    or min(info["width"], info["height"]) < imaging.MIN_SIDE
                    or xref in repeated
                    or shown.width * shown.height >= COVER_SHARE * page.rect.get_area()
                ):
                    continue
                # Pixel boxes map onto the whole drawn rect, even the part off the page
                rect = bbox if xref else shown
                data = _image_bytes(doc, xref) if xref else _inline_bytes(page, shown)
                out.jobs.append(
                    RecognizeJob(
                        "image",
                        replace(src, page=i, img=n),
                        data=data,
                        rect=(rect.x0, rect.y0, rect.x1, rect.y1),
                    )
                )
    out.hidden = len(scan(str(path)))
    if out.hidden:
        out.needs.append(f"hidden {out.hidden} spans")
    return out
