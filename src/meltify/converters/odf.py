from __future__ import annotations

import base64
import binascii
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from urllib.parse import unquote, urlsplit

from meltify.converters import Block, Converted
from meltify.converters.embeds import VECTOR, Embeds
from meltify.converters.limits import MAX_MEMBER_BYTES, human_bytes
from meltify.converters.tables import escape_cell, grid
from meltify.converters.text import numbered_lines
from meltify.converters.xmlsafe import parse
from meltify.evidence import Src

TEXT = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
TABLE = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
DRAW = "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
OFFICE = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"
PRESENTATION = "urn:oasis:names:tc:opendocument:xmlns:presentation:1.0"
CHART = "urn:oasis:names:tc:opendocument:xmlns:chart:1.0"
XLINK = "http://www.w3.org/1999/xlink"

# Spreadsheets pad to the sheet edge with one element repeated a million times
MAX_REPEAT = 1000
MAX_CELLS = 200_000


def _q(ns: str, name: str) -> str:
    return f"{{{ns}}}{name}"


P, H = _q(TEXT, "p"), _q(TEXT, "h")
SPACE, TAB, BREAK = _q(TEXT, "s"), _q(TEXT, "tab"), _q(TEXT, "line-break")
# Tracked deletions and hidden text aren't part of what the reader sees
SKIP = {_q(TEXT, "tracked-changes"), _q(TEXT, "hidden-text"), _q(TEXT, "hidden-paragraph")}
TBL, ROW, CELL = _q(TABLE, "table"), _q(TABLE, "table-row"), _q(TABLE, "table-cell")
COVERED = _q(TABLE, "covered-table-cell")
REPEAT_COLS, REPEAT_ROWS = _q(TABLE, "number-columns-repeated"), _q(TABLE, "number-rows-repeated")
FRAME, IMAGE, HREF = _q(DRAW, "frame"), _q(DRAW, "image"), _q(XLINK, "href")
# Charts, formulas and OLE objects keep their content in a sub-document, not a picture
OBJECTS = {_q(DRAW, "object"), _q(DRAW, "object-ole")}
BINARY = _q(OFFICE, "binary-data")
CHART_ROOT, CHART_TITLE, AXIS = _q(CHART, "chart"), _q(CHART, "title"), _q(CHART, "axis")


def _raw(text: str, out: list[str]) -> None:
    # ODF collapses source whitespace across element boundaries too
    text = re.sub(r"\s+", " ", text)
    if text.startswith(" ") and out and out[-1].endswith(" "):
        text = text[1:]
    if text:
        out.append(text)


def _inline(el: ET.Element, out: list[str]) -> None:
    # Real spaces, tabs and breaks are spelled as elements
    if el.tag in SKIP:
        return
    if el.tag == SPACE:
        out.append(" " * int(el.get(_q(TEXT, "c"), "1")))
    elif el.tag == TAB:
        out.append("\t")
    elif el.tag == BREAK:
        out.append("\n")
    elif el.text:
        _raw(el.text, out)
    for child in el:
        _inline(child, out)
        if child.tail:
            _raw(child.tail, out)


def text_of(el: ET.Element) -> str:
    out: list[str] = []
    _inline(el, out)
    return "".join(out).strip()


def _cell_text(cell: ET.Element) -> str:
    return escape_cell(" ".join(t for p in cell.iter() if p.tag in (P, H) and (t := text_of(p))))


def _rows(table: ET.Element):
    """Rows of this table only, through header and group wrappers but not into subtables"""
    for child in table:
        if child.tag == ROW:
            yield child
        elif child.tag != TBL:
            yield from _rows(child)


def cells_of(table: ET.Element, notes: set[str]) -> dict[tuple[int, int], str]:
    """Non-empty cells keyed by 1-based (row, column), expanding repeats up to a cap"""
    cells: dict[tuple[int, int], str] = {}
    r = 0
    for row in _rows(table):
        n_rows = int(row.get(REPEAT_ROWS, "1"))
        values: list[tuple[int, str]] = []
        c = 0
        for cell in row:
            if cell.tag not in (CELL, COVERED):
                continue
            n_cols = int(cell.get(REPEAT_COLS, "1"))
            text = _cell_text(cell) if cell.tag == CELL else ""
            if text:
                if n_cols > MAX_REPEAT:
                    notes.add(f"repeated cells cut at {MAX_REPEAT}")
                values += [(c + i + 1, text) for i in range(min(n_cols, MAX_REPEAT))]
            c += n_cols
        if values:
            if n_rows > MAX_REPEAT:
                notes.add(f"repeated rows cut at {MAX_REPEAT}")
            for i in range(min(n_rows, MAX_REPEAT)):
                if len(cells) + len(values) > MAX_CELLS:
                    notes.add(f"stopped at {MAX_CELLS} cells")
                    return cells
                cells.update({(r + i + 1, col): v for col, v in values})
        r += n_rows
    return cells


def _tables(body: ET.Element, src: Src, out: Converted, notes: set[str]) -> None:
    for i, table in enumerate(body.iter(TBL), start=1):
        cells = cells_of(table, notes)
        if cells:
            name = table.get(_q(TABLE, "name")) or f"Table{i}"
            out.blocks.append(Block(replace(src, sheet=name), grid(cells)))


def _paragraphs(el: ET.Element, found: list[ET.Element]) -> None:
    """Headings and paragraphs in reading order, leaving tables to their own blocks"""
    for child in el:
        if child.tag in SKIP or child.tag == TBL:
            continue
        if child.tag in (H, P):
            found.append(child)
        else:
            _paragraphs(child, found)


def _text(body: ET.Element, src: Src, out: Converted) -> list[ET.Element]:
    # The cite line is the paragraph's position in the document, empty ones included
    found: list[ET.Element] = []
    _paragraphs(body, found)
    lines = []
    for n, el in enumerate(found, start=1):
        level = int(el.get(_q(TEXT, "outline-level"), "1"))
        mark = "#" * min(level, 6) + " " if el.tag == H else ""
        if t := text_of(el):
            lines.append((n, f"{mark}{t}"))
    out.blocks += numbered_lines(lines, src, len(str(len(found))))
    return found


def _pages(body: ET.Element) -> list[ET.Element]:
    return body.findall(_q(DRAW, "page"))


def _slides(body: ET.Element, src: Src, out: Converted, unit: str) -> None:
    """Text of each slide or drawing page, cited by `unit` which is slide or page"""
    notes_tag = _q(PRESENTATION, "notes")
    for n, page in enumerate(_pages(body), start=1):
        spoken_ps = [p for notes in page.iter(notes_tag) for p in notes.iter(P)]
        spoken = [t for p in spoken_ps if (t := text_of(p))]
        skip = set(map(id, spoken_ps))
        shown = [
            t for p in page.iter() if p.tag in (P, H) and id(p) not in skip and (t := text_of(p))
        ]
        text = "\n".join(shown)
        if spoken:
            text += "\n\nNotes:\n" + "\n".join(spoken)
        if text.strip():
            out.blocks.append(Block(replace(src, **{unit: n}), text.strip()))


def _frames(el: ET.Element) -> Iterator[ET.Element]:
    """Frames holding a picture or an object in document order, outside hidden text"""
    for child in el:
        if child.tag in SKIP:
            continue
        if child.tag == FRAME and any(c.tag == IMAGE or c.tag in OBJECTS for c in child):
            yield child
        yield from _frames(child)


def chart_text(chart: ET.Element) -> str | None:
    """A chart's title, axes and the local table it draws from, None when that has no rows

    The table's first row names the series and its first column holds the categories
    """
    title = next((text_of(t) for t in chart.findall(CHART_TITLE)), "")
    lines = [f"Chart: {title}" if title else "Chart"]
    axes = [
        t for axis in chart.iter(AXIS) for el in axis.findall(CHART_TITLE) if (t := text_of(el))
    ]
    if axes:
        lines.append("Axes: " + ", ".join(axes))
    table = chart.find(f".//{TBL}")
    cells = {} if table is None else cells_of(table, set())
    rows = sorted({r for r, _ in cells} - {1})
    if not rows:
        return None
    width = max(c for _, c in cells)
    head = [cells.get((1, c), "") for c in range(1, width + 1)]
    lines.append("| " + " | ".join([head[0] or "category", *head[1:]]) + " |")
    lines.append("|---" * width + "|")
    for r in rows:
        lines.append("| " + " | ".join(cells.get((r, c), "") for c in range(1, width + 1)) + " |")
    return "\n".join(lines)


def _object(z: zipfile.ZipFile, href: str, src: Src, embeds: Embeds) -> None:
    """A chart object's own content.xml read as a block, anything else listed as a need"""
    folder = posixpath.normpath(unquote(urlsplit(href).path))
    part = posixpath.join(folder, "content.xml")
    if urlsplit(href).scheme or folder.startswith(("..", "/")) or part not in z.NameToInfo:
        embeds.skipped["embedded object"] += 1
        return
    info = z.getinfo(part)
    try:
        if info.file_size > MAX_MEMBER_BYTES:
            raise ValueError("over the size limit")
        chart = parse(z.read(info)).find(f".//{CHART_ROOT}")
    except (ET.ParseError, ValueError):
        embeds.skipped["unreadable embedded object"] += 1
        return
    if chart is None:
        # Formulas and OLE objects, which have no text layer meltify reads
        embeds.skipped["embedded object"] += 1
        return
    text = chart_text(chart)
    if text is None:
        embeds.uncached.append(src)
    else:
        embeds.blocks.append(Block(src, text))


def _picture(z: zipfile.ZipFile, frame: ET.Element, src: Src, embeds: Embeds) -> None:
    if (obj := frame.find(_q(DRAW, "object"))) is not None:
        _object(z, obj.get(HREF, ""), src, embeds)
        return
    if any(c.tag in OBJECTS for c in frame):
        embeds.skipped["embedded object"] += 1
        return
    images = frame.findall(IMAGE)
    # LibreOffice pairs an SVG with a PNG fallback in one frame, so the raster one wins
    image = next(
        (i for i in images if posixpath.splitext(i.get(HREF, ""))[1].lower() not in VECTOR),
        images[0],
    )
    if (binary := image.find(BINARY)) is not None:
        try:
            embeds.add(src, base64.b64decode(binary.text or ""))
        except binascii.Error:
            embeds.skipped["unreadable image"] += 1
        return
    href = image.get(HREF, "")
    member = posixpath.normpath(unquote(urlsplit(href).path))
    if posixpath.splitext(member)[1].lower() == ".svm":
        # StarView metafiles are LibreOffice's own vector format, which nothing else draws
        embeds.skipped["svm image"] += 1
        return
    # A URL or a path out of the package points at a file the document doesn't carry
    outside = bool(urlsplit(href).scheme) or member.startswith(("..", "/"))
    embeds.member(z, src, {href: None if outside else member}, href)


def _scope(
    z: zipfile.ZipFile, el: ET.Element, src: Src, embeds: Embeds, para: dict[int, int] | None = None
) -> None:
    """Pictures under one sheet, page or document, numbered in document order"""
    for i, frame in enumerate(_frames(el), start=1):
        at = (para or {}).get(id(frame))
        _picture(z, frame, replace(src, para=at, img=i), embeds)


def convert(path: Path, src: Src) -> Converted:
    out = Converted("odf")
    notes: set[str] = set()
    embeds = Embeds()
    with zipfile.ZipFile(path) as z:
        info = z.getinfo("content.xml")
        if info.file_size > MAX_MEMBER_BYTES:
            raise ValueError(f"content.xml is over the {human_bytes(MAX_MEMBER_BYTES)} limit")
        body = parse(z.read(info)).find(_q(OFFICE, "body"))
        if body is None:
            return out
        if (sheet := body.find(_q(OFFICE, "spreadsheet"))) is not None:
            _tables(sheet, src, out, notes)
            for i, table in enumerate(sheet.findall(TBL), start=1):
                name = table.get(_q(TABLE, "name")) or f"Table{i}"
                _scope(z, table, replace(src, sheet=name), embeds)
        elif (deck := body.find(_q(OFFICE, "presentation"))) is not None:
            _slides(deck, src, out, "slide")
            for n, page in enumerate(_pages(deck), start=1):
                _scope(z, page, replace(src, slide=n), embeds)
        elif (drawing := body.find(_q(OFFICE, "drawing"))) is not None:
            _slides(drawing, src, out, "page")
            for n, page in enumerate(_pages(drawing), start=1):
                _scope(z, page, replace(src, page=n), embeds)
        else:
            paras = _text(body, src, out)
            _tables(body, src, out, notes)
            # A picture cites the paragraph it sits in, the same number its text line shows
            at = {id(f): n for n, p in enumerate(paras, start=1) for f in p.iter(FRAME)}
            _scope(z, body, src, embeds, at)
    embeds.into(out)
    out.needs += sorted(notes)
    return out
