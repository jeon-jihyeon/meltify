from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import replace
from pathlib import Path

from openpyxl.utils import get_column_letter

from meltify.converters import Block, Converted
from meltify.converters.text import BLOCK_LINES
from meltify.evidence import Src

TEXT = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
TABLE = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
DRAW = "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
OFFICE = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"
PRESENTATION = "urn:oasis:names:tc:opendocument:xmlns:presentation:1.0"

# Same per-member ceiling as archives, since content.xml is a zip member too
MAX_XML = 256 * 1024 * 1024
# Spreadsheets pad to the sheet edge with one element repeated a million times
MAX_REPEAT = 1000
MAX_CELLS = 200_000
ENTITY = (b"<!ENTITY", "<!ENTITY".encode("utf-16-le"), "<!ENTITY".encode("utf-16-be"))


def _q(ns: str, name: str) -> str:
    return f"{{{ns}}}{name}"


P, H = _q(TEXT, "p"), _q(TEXT, "h")
SPACE, TAB, BREAK = _q(TEXT, "s"), _q(TEXT, "tab"), _q(TEXT, "line-break")
# Tracked deletions and hidden text aren't part of what the reader sees
SKIP = {_q(TEXT, "tracked-changes"), _q(TEXT, "hidden-text"), _q(TEXT, "hidden-paragraph")}
TBL, ROW, CELL = _q(TABLE, "table"), _q(TABLE, "table-row"), _q(TABLE, "table-cell")
COVERED = _q(TABLE, "covered-table-cell")
REPEAT_COLS, REPEAT_ROWS = _q(TABLE, "number-columns-repeated"), _q(TABLE, "number-rows-repeated")


def parse_xml(data: bytes) -> ET.Element:
    """Parse XML that came from an untrusted file, refusing entity declarations"""
    if any(e in data for e in ENTITY):
        raise ValueError("refusing XML with <!ENTITY declarations")
    return ET.fromstring(data)


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
    text = " ".join(t for p in cell.iter() if p.tag in (P, H) and (t := text_of(p)))
    return text.replace("|", "\\|").replace("\n", " ")


def _rows(table: ET.Element):
    """Rows of this table only, through header and group wrappers but not into subtables"""
    for child in table:
        if child.tag == ROW:
            yield child
        elif child.tag != TBL:
            yield from _rows(child)


def grid(table: ET.Element, notes: set[str]) -> dict[tuple[int, int], str]:
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


def markdown_grid(cells: dict[tuple[int, int], str]) -> str:
    # Row numbers and column letters let a reader cite a single cell
    width = max(c for _, c in cells)
    lines = [
        "| row | " + " | ".join(get_column_letter(i) for i in range(1, width + 1)) + " |",
        "|---" * (width + 1) + "|",
    ]
    for r in sorted({r for r, _ in cells}):
        values = [cells.get((r, c), "") for c in range(1, width + 1)]
        lines.append(f"| {r} | " + " | ".join(values) + " |")
    return "\n".join(lines)


def _tables(body: ET.Element, src: Src, out: Converted, notes: set[str]) -> None:
    for i, table in enumerate(body.iter(TBL), start=1):
        cells = grid(table, notes)
        if cells:
            name = table.get(_q(TABLE, "name")) or f"Table{i}"
            out.blocks.append(Block(replace(src, sheet=name), markdown_grid(cells)))


def _paragraphs(el: ET.Element, found: list[tuple[str, str]]) -> None:
    """Headings and paragraphs in reading order, leaving tables to their own blocks"""
    for child in el:
        if child.tag in SKIP or child.tag == TBL:
            continue
        if child.tag == H:
            level = int(child.get(_q(TEXT, "outline-level"), "1"))
            found.append(("#" * min(level, 6) + " ", text_of(child)))
        elif child.tag == P:
            found.append(("", text_of(child)))
        else:
            _paragraphs(child, found)


def _text(body: ET.Element, src: Src, out: Converted) -> None:
    # The cite line is the paragraph's position in the document, empty ones included
    found: list[tuple[str, str]] = []
    _paragraphs(body, found)
    lines = [(n, f"{mark}{t}") for n, (mark, t) in enumerate(found, start=1) if t]
    width = len(str(len(found)))
    for start in range(0, len(lines), BLOCK_LINES):
        chunk = lines[start : start + BLOCK_LINES]
        body_text = "\n".join(f"{n:>{width}}| {t}" for n, t in chunk)
        out.blocks.append(Block(replace(src, line=chunk[0][0]), body_text))


def _slides(body: ET.Element, src: Src, out: Converted) -> None:
    notes_tag = _q(PRESENTATION, "notes")
    for n, page in enumerate(body.findall(_q(DRAW, "page")), start=1):
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
            out.blocks.append(Block(replace(src, slide=n), text.strip()))


def convert(path: Path, src: Src) -> Converted:
    with zipfile.ZipFile(path) as z:
        info = z.getinfo("content.xml")
        if info.file_size > MAX_XML:
            raise ValueError(f"content.xml is {info.file_size} bytes, over the {MAX_XML} limit")
        root = parse_xml(z.read(info))
    body = root.find(_q(OFFICE, "body"))
    out = Converted("odf")
    if body is None:
        return out
    notes: set[str] = set()
    if (sheet := body.find(_q(OFFICE, "spreadsheet"))) is not None:
        _tables(sheet, src, out, notes)
    elif (deck := body.find(_q(OFFICE, "presentation"))) is not None:
        _slides(deck, src, out)
    else:
        _text(body, src, out)
        _tables(body, src, out, notes)
    out.needs += sorted(notes)
    return out
