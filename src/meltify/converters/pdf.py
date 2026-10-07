from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from meltify.converters import Block, Child, Converted, RecognizeJob
from meltify.converters.limits import MAX_MEMBER_BYTES, human_bytes
from meltify.evidence import Src
from meltify.needs import error_note

if TYPE_CHECKING:
    from meltify.forensics.pdf import Marks

# A page with less text than this is probably a scan
SCAN_CHARS = 20
# Drawn smaller than about 50 by 50 pt, an image is a bullet, an icon or a stamp
MIN_AREA_PT = 2500
# In a 3+ page document, an image on more than half the pages is a logo or a letterhead
REPEAT_SHARE = 0.5
# An image this much of a page is a scan, read already when a text layer lies over it
COVER_SHARE = 0.8
# Pillow reads these straight from the PDF stream. Anything else goes through a pixmap
DIRECT = {"png", "jpeg", "jpg"}
# Other paged formats MuPDF opens, named by the filetype it expects. EPUB has its own
# converter, which keeps its chapters and links
MUPDF = {".xps", ".oxps", ".fb2", ".cbz", ".mobi"}
# FreeText shows its text on the page and a popup repeats its parent's
SHOWN = {"FreeText", "Popup"}


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


class _Xrefs:
    """Which xref each drawn image comes from, worked out only for pages that ask

    PyMuPDF matches a drawn image to its xref by decoding and hashing every image on the
    page. On a scanned PDF that's nearly all the convert time, and scan pages never use it
    """

    def __init__(self, doc: Any) -> None:
        self.doc = doc
        self._drawn: dict[int, list[int]] = {}
        self._listed: list[set[int]] | None = None
        self._repeated: dict[int, bool] = {}

    def drawn(self, page: Any) -> list[int]:
        """Xref of each image in get_image_info order, 0 for inline images and non-PDFs"""
        if page.number not in self._drawn:
            infos = page.get_image_info(xrefs=True)
            self._drawn[page.number] = [i.get("xref", 0) for i in infos]
        return self._drawn[page.number]

    def repeated(self, xref: int) -> bool:
        """Whether a 3+ page document draws the image on more than half its pages"""
        pages = self.doc.page_count
        if not xref or pages < 3:
            return False
        if xref not in self._repeated:
            if self._listed is None:
                self._listed = [{img[0] for img in page.get_images()} for page in self.doc]
            # PyMuPDF only matches a drawn image to an xref its page lists, so the cheap
            # listing settles most images without decoding any
            listing = [n for n, xrefs in enumerate(self._listed) if xref in xrefs]
            if len(listing) / pages > REPEAT_SHARE:
                listing = [n for n in listing if xref in self.drawn(self.doc[n])]
            self._repeated[xref] = len(listing) / pages > REPEAT_SHARE
        return self._repeated[xref]


def _layer_chars(marks: Marks, area: Any) -> int:
    """Chars of unseen text over `area`, the way OCR tools make a scan searchable

    They write the text invisible or paint the scan over it, while a visible header on
    top of a scan is no reading of the pixels below. Counting stops at SCAN_CHARS, all the
    caller asks about
    """
    import pymupdf

    n = 0
    for s in marks.trace:
        unseen = s["type"] == 3 or s["opacity"] == 0
        rect = pymupdf.Rect(s["bbox"])
        if not rect.intersects(area):
            continue
        if unseen or marks.images.covered(s["seqno"], rect):
            n += len(s["chars"])
            if n >= SCAN_CHARS:
                break
    return n


def _notes(page: Any, src: Src) -> list[Block]:
    """Comments on annotations, which page text leaves out"""
    out = []
    for annot in page.annots():
        kind = annot.type[1]
        info = annot.info
        text = (info.get("content") or "").strip()
        if kind in SHOWN or not text:
            continue
        by = f" by {info['title']}" if info.get("title") else ""
        r = annot.rect
        at = replace(src, bbox=tuple(round(v, 1) for v in (r.x0, r.y0, r.x1, r.y1)), unit="pt")
        out.append(Block(at, f"[{kind}{by}] {text}"))
    return out


def _attachments(doc: Any, src: Src, out: Converted) -> None:
    """Files embedded in the document or pinned to a page, melted as nested items

    One broken or oversized attachment is listed as a need and the rest still melt
    """
    import pymupdf

    too_big = f"over {human_bytes(MAX_MEMBER_BYTES)}"

    def take(name: str, info: Callable[[], dict], read: Callable[[], bytes]) -> None:
        try:
            found = info()
            name = found.get("filename") or name
            # Size is the unpacked length and may be missing, so the packed length bounds it too
            if max(found.get("size", -1), found.get("length", -1)) > MAX_MEMBER_BYTES:
                out.needs.append(f"attachment {name} not read ({too_big})")
                return
            data = read()
        except Exception as e:  # noqa: BLE001
            out.needs.append(f"attachment {name} not read ({error_note(e)})")
            return
        if len(data) > MAX_MEMBER_BYTES:
            # Declared sizes can lie, so the bytes actually read are checked too
            out.needs.append(f"attachment {name} not read ({too_big})")
            return
        out.children.append(Child(name, src, data=data))

    for name in doc.embfile_names():
        take(name, lambda n=name: doc.embfile_info(n), lambda n=name: doc.embfile_get(n))
    for page in doc:
        for annot in page.annots(types=[pymupdf.PDF_ANNOT_FILE_ATTACHMENT]):
            name = f"p{page.number + 1}-attachment"
            take(name, lambda a=annot: a.file_info, lambda a=annot: a.get_file())


def convert(path: Path, src: Src) -> Converted:
    import pymupdf

    from meltify import imaging
    from meltify.forensics.pdf import DEFAULT_LIMITS, Marks, scan_page

    suffix = path.suffix.lower()
    out = Converted("pdf")
    with pymupdf.open(path, filetype=suffix[1:] if suffix in MUPDF else None) as doc:
        xrefs = _Xrefs(doc)
        for i, page in enumerate(doc, start=1):
            # The hidden text check and the scan layer check read the same draw log
            marks = Marks(page)
            if suffix not in MUPDF:
                out.hidden += sum(1 for _ in scan_page(page, i, DEFAULT_LIMITS, marks))
            text = page.get_text("text").strip()
            if text:
                out.blocks.append(Block(replace(src, page=i), text))
            if doc.is_pdf:
                out.blocks += _notes(page, replace(src, page=i))
            if len(text) < SCAN_CHARS:
                # A full-page image with no text is a scan. OCR the rendered page instead of
                # its images, so text drawn on top of them is read too
                out.jobs.append(RecognizeJob("page", replace(src, page=i), path=path))
                continue
            for n, info in enumerate(page.get_image_info(), start=1):
                bbox = pymupdf.Rect(info["bbox"])
                shown = bbox & page.rect
                if (
                    shown.is_empty
                    or shown.width * shown.height < MIN_AREA_PT
                    or min(info["width"], info["height"]) < imaging.MIN_SIDE
                ):
                    continue
                if (
                    shown.width * shown.height >= COVER_SHARE * page.rect.get_area()
                    and _layer_chars(marks, shown) >= SCAN_CHARS
                ):
                    continue
                # Only PDF images have an xref, so the others render like inline images
                xref = xrefs.drawn(page)[n - 1]
                if xrefs.repeated(xref):
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
        if doc.is_pdf:
            _attachments(doc, src, out)
    if out.hidden:
        out.needs.append(f"hidden {out.hidden} spans")
    return out
