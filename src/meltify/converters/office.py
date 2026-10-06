"""Word and PowerPoint packages: markitdown's text cited by paragraph or slide, plus the
pictures, charts and diagrams each one shows
"""

from __future__ import annotations

import functools
import importlib.util
import posixpath
import re
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from meltify.converters import Block, Converted, sheet
from meltify.converters.embeds import Embeds
from meltify.converters.limits import checked, read_part
from meltify.converters.ooxml import (
    ALTERNATE,
    HANCOM,
    NS,
    PARA,
    R_ID,
    Package,
    branch,
    hancom_need,
    integer,
    objects,
    package_of,
    paragraphs,
    rels,
    with_parts,
)
from meltify.converters.ooxml_charts import (
    extent,
    frame,
    render_charts,
    render_docx_charts,
    take,
)
from meltify.converters.text import numbered_lines
from meltify.converters.xmlsafe import parse, zip_xml
from meltify.evidence import Src
from meltify.safe import MissingTool

# The only main part types python-pptx opens, so slideshows and templates get patched
PPTX_MAIN = "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"
PPTX_OPENS = {PPTX_MAIN, "application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml"}
# Paragraph numbers written into a docx copy for markitdown, in private use characters
# its markdown leaves alone
NUMBER = "\ue000{}\ue001"
NUMBERED = re.compile("\ue000(\\d+)\ue001")
# A w:p start tag. Byte order is document order, the order ElementTree iterates them in
P_START = re.compile(r"<w:p(?=[\s/>])[^>]*>")
NOTES = ("footnotes.xml", "endnotes.xml")
SLIDE_MARK = re.compile(r"<!-- Slide number: (\d+) -->")


def docx_images(path: Path, src: Src) -> Embeds:
    embeds = Embeds()
    charts: dict[Src, tuple[str, int, int]] = {}
    found = package_of(path)
    part = found.main if found else "word/document.xml"
    with zipfile.ZipFile(path) as z:
        targets = rels(z, part)
        root = zip_xml(z, part)
        paras = paragraphs(root)
        for i, (kind, rid, up) in enumerate(objects(root), start=1):
            para = next((paras[id(a)] for a in reversed(up) if a.tag == PARA), None)
            at = replace(src, para=para, img=i)
            take(embeds, z, kind, rid, targets, at)
            if kind == "chart" and rid:
                charts[at] = (rid, *extent(up))
    render_docx_charts(path, part, embeds, charts)
    return embeds


def pptx_images(path: Path, src: Src) -> Embeds:
    embeds = Embeds()
    regions: dict[Src, tuple[int, tuple[float, ...]]] = {}
    found = package_of(path)
    part = found.main if found else "ppt/presentation.xml"
    with zipfile.ZipFile(path) as z:
        targets = rels(z, part)
        prs = zip_xml(z, part)
        size = prs.find("p:sldSz", NS)
        slide_size = (0, 0) if size is None else (integer(size.get("cx")), integer(size.get("cy")))
        slides = prs.find("p:sldIdLst", NS)
        page = 0
        for n, sld in enumerate([] if slides is None else slides, start=1):
            slide = targets.get(sld.get(R_ID) or "")
            if slide is None:
                continue
            root = zip_xml(z, slide)
            # A PDF export leaves hidden slides out, so they have no page to crop from
            shown = root.get("show") != "0"
            page += shown
            pics = rels(z, slide)
            for i, (kind, rid, up) in enumerate(objects(root), start=1):
                at = replace(src, slide=n, img=i)
                take(embeds, z, kind, rid, pics, at)
                if kind == "chart" and shown and (clip := frame(up, slide_size)):
                    regions[at] = (page, clip)
    render_charts(path, embeds, regions, page)
    return embeds


def _slides_without_charts() -> Any:
    from markitdown.converters import PptxConverter

    class Slides(PptxConverter):
        # Chart parts become their own cited blocks, so the slide text leaves them out
        def _convert_chart_to_markdown(self, chart: object) -> str:
            return ""

    return Slides()


@functools.cache
def _converter(family: str) -> Any:
    """markitdown's converter for one family, made once and shared across threads

    Calling it straight skips the MarkItDown wrapper, whose every instance and call runs
    Magika to guess a type the family already names. The converters keep no state between
    calls
    """
    from markitdown import converters

    made: dict[str, Callable[[], Any]] = {
        "docx": converters.DocxConverter,
        "pptx": _slides_without_charts,
        "epub": converters.EpubConverter,
        "msg": converters.OutlookMsgConverter,
    }
    return made[family]()


def markdown_of(family: str, path: Path) -> str:
    """Markdown markitdown makes of a docx, pptx, epub or msg file

    Lines are tidied the way the MarkItDown wrapper tidies them, trailing spaces dropped
    and runs of blank lines cut to one
    """
    from markitdown import StreamInfo

    ext = f".{family}"
    with path.open("rb") as f:
        result = _converter(family).convert(f, StreamInfo(extension=ext), file_extension=ext)
    text = "\n".join(line.rstrip() for line in re.split(r"\r?\n", result.markdown))
    return re.sub(r"\n{3,}", "\n\n", text)


def _as_presentation(path: Path, content_type: str, out: Path) -> None:
    """A copy with the main part typed as a plain deck, for slideshows and templates"""
    with zipfile.ZipFile(path) as z:
        types = read_part(z, "[Content_Types].xml")
    with_parts(
        path, {"[Content_Types].xml": types.replace(content_type.encode(), PPTX_MAIN.encode())}, out
    )


def _text_numbers(root: ET.Element, start: int) -> list[int | None]:
    """The number each w:p start tag gets, in document order, counting on from `start`

    Numbers match `paragraphs`, so text cites the paragraphs pictures cite. mammoth reads
    the Fallback of an mc:AlternateContent, so a Fallback that mirrors the shown Choice
    paragraph for paragraph takes the Choice's numbers
    """
    numbers = {k: n + start for k, n in paragraphs(root).items()}
    for alternate in root.iter(ALTERNATE):
        if (chosen := branch(alternate)) is None:
            continue
        mine = [numbers[id(p)] for p in chosen.iter(PARA) if id(p) in numbers]
        for other in alternate:
            theirs = [p for p in other.iter(PARA) if id(p) not in numbers]
            if other is not chosen and len(theirs) == len(mine):
                numbers.update({id(p): n for p, n in zip(theirs, mine, strict=True)})
    return [numbers.get(id(p)) for p in root.iter(PARA)]


def _numbered(data: bytes, numbers: list[int | None]) -> bytes | None:
    """The part with each numbered paragraph's number written as its first run

    None when its start tags don't line up with the parsed paragraphs, like a part that
    isn't UTF-8 or binds the main namespace to another prefix
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    tags = list(P_START.finditer(text))
    if len(tags) != len(numbers):
        return None
    out: list[str] = []
    at = 0
    for tag, n in zip(tags, numbers, strict=True):
        # An empty paragraph shows no text, so it needs no number
        if n is None or tag.group().endswith("/>"):
            continue
        out += [text[at : tag.end()], f"<w:r><w:t>{NUMBER.format(n)}</w:t></w:r>"]
        at = tag.end()
    out.append(text[at:])
    return "".join(out).encode()


def _text_copy(path: Path, part: str, out: Path) -> None:
    """A copy of a Word package for markitdown whose paragraphs carry their numbers

    A part whose numbers can't be written stays as it is. Footnotes and endnotes continue the
    count after the body, the order mammoth adds them in. Pictures in the media folder go
    empty, since mammoth base64 encodes each one into a data URI that markitdown cuts back
    to its type, and docx_images reads them from the original
    """
    with zipfile.ZipFile(path) as z:
        notes = [t for t in rels(z, part).values() if t and posixpath.basename(t) in NOTES]
        changed: dict[str, bytes] = {}
        count = 0
        for name in [part, *notes]:
            try:
                data = read_part(z, name)
                root = parse(data)
            except (KeyError, ET.ParseError, ValueError):
                continue
            numbers = _text_numbers(root, count)
            count += len(paragraphs(root))
            if (numbered := _numbered(data, numbers)) is not None:
                changed[name] = numbered
        media = posixpath.join(posixpath.dirname(part), "media/")
        changed |= {n: b"" for n in z.namelist() if n.startswith(media)}
    with_parts(path, changed, out)


def _paragraph_blocks(text: str, src: Src) -> list[Block]:
    """Word text cited by paragraph, from the numbers `_text_copy` wrote

    A line takes the number of the paragraph it starts in, and one without a number
    belongs to the paragraph above it. A markdown table becomes a block of its own,
    cited by the first paragraph inside it, since its header row may be made up
    """
    first = NUMBERED.search(text)
    n = int(first.group(1)) if first else 1
    # Each line with the paragraph it belongs to and whether it named that one itself
    rows: list[tuple[int, bool, str]] = []
    for raw in text.splitlines():
        found = NUMBERED.findall(raw)
        line = int(found[0]) if found else n
        n = int(found[-1]) if found else n
        if (clean := NUMBERED.sub("", raw).rstrip()).strip():
            rows.append((line, bool(found), clean))
    width = len(str(max((r for r, _, _ in rows), default=1)))
    blocks: list[Block] = []
    lines: list[tuple[int, bool, str]] = []
    table: list[tuple[int, bool, str]] = []

    def flush() -> None:
        blocks.extend(numbered_lines([(r, t) for r, _, t in lines], src, width))
        lines.clear()
        if table:
            at = next((r for r, named, _ in table if named), table[0][0])
            blocks.append(Block(replace(src, line=at), "\n".join(t for _, _, t in table)))
            table.clear()

    for row in rows:
        is_table = row[2].lstrip().startswith("|")
        if is_table != bool(table) and (lines or table):
            flush()
        (table if is_table else lines).append(row)
    flush()
    return blocks


def _slide_blocks(text: str, src: Src) -> list[Block]:
    """Deck text cut at the marker markitdown writes ahead of each slide, cited by slide"""
    parts = SLIDE_MARK.split(text)
    blocks = [Block(src, parts[0].strip())] if parts[0].strip() else []
    for n, body in zip(parts[1::2], parts[2::2], strict=True):
        if body.strip():
            blocks.append(Block(replace(src, slide=int(n)), body.strip()))
    return blocks


def _markdown(path: Path, family: str, found: Package | None) -> str:
    if found is None:
        return markdown_of(family, path)
    with tempfile.TemporaryDirectory(prefix=f"meltify-{family}-") as tmp:
        copy = Path(tmp) / f"text.{family}"
        if family == "docx":
            _text_copy(path, found.main, copy)
            return markdown_of(family, copy)
        if found.content_type in PPTX_OPENS:
            return markdown_of(family, path)
        _as_presentation(path, found.content_type, copy)
        return markdown_of(family, copy)


def convert(path: Path, src: Src) -> Converted:
    suffix = path.suffix.lower()
    found = package_of(path)
    if found is None and suffix in HANCOM:
        return Converted("office", needs=[hancom_need(suffix)])
    if found is not None and found.family == "xlsx":
        return sheet.convert(path, src)
    if found is not None and found.family == "xlsb":
        return sheet.convert_binary(path, src)
    if importlib.util.find_spec("markitdown") is None:
        if found is None:
            raise MissingTool("markitdown", "meltify doctor --install office")
        # Pictures come straight from the zip, so they're still worth reading without the text
        out = Converted("office", needs=["markitdown"])
    else:
        if zipfile.is_zipfile(path):
            # markitdown reads the XML parts without limits of its own
            with zipfile.ZipFile(path) as z:
                for info in z.infolist():
                    if info.filename.endswith((".xml", ".rels")):
                        checked(info)
        # Without a readable package the name says which converter to try
        family = found.family if found else "pptx" if suffix.startswith(".p") else "docx"
        text = _markdown(path, family, found)
        if found is not None and found.family == "pptx":
            blocks = _slide_blocks(text, src)
        elif found is not None and NUMBERED.search(text):
            blocks = _paragraph_blocks(text, src)
        else:
            blocks = [Block(src, text)] if text.strip() else []
        out = Converted("office", blocks)
    if found is not None:
        pictures = docx_images if found.family == "docx" else pptx_images
        pictures(path, src).into(out)
    return out
