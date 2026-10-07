"""iWork '09 documents, which kept their content as plain XML, gzipped or not"""

from __future__ import annotations

import xml.etree.ElementTree as ET
import zlib
from collections.abc import Iterator
from dataclasses import replace

from meltify.converters import Block, Converted
from meltify.converters.iwork_bundle import Bundle
from meltify.converters.limits import MAX_MEMBER_BYTES, human_bytes
from meltify.converters.tables import escape_cell, grid
from meltify.converters.text import numbered_lines
from meltify.converters.xmlsafe import parse
from meltify.evidence import Src

SF = "{http://developer.apple.com/namespaces/sf}"
SFA = "{http://developer.apple.com/namespaces/sfa}"
SL = "{http://developer.apple.com/namespaces/sl}"
KEY = "{http://developer.apple.com/namespaces/keynote2}"
PARA, TABLE = f"{SF}p", f"{SF}tabular-model"
STORAGE, ATTACHMENT, ATTACHMENT_REF = f"{SF}text-storage", f"{SF}attachment", f"{SF}attachment-ref"
# Page furniture and comments keep their own text, outside the body flow, and section
# prototypes are the template's sample pages
FURNITURE = {f"{SF}{t}" for t in ("header", "footer", "footnotes", "annotations")}
FURNITURE.add(f"{SL}section-prototypes")
GHOST = {f"{SF}ghost-text", f"{SF}ghost-text-ref"}


def slide_text(shown: list[str], spoken: list[str]) -> str:
    body = "\n".join(shown)
    if spoken:
        body += "\n\nNotes:\n" + "\n".join(spoken)
    return body.strip()


def _xml_text(el: ET.Element) -> str:
    # Template placeholder text shows before anyone types, so it isn't content
    parts: list[str] = []
    # Elements left to walk, each with its tail queued behind it. A stack instead of
    # recursion, so deep XML can't blow the call stack
    stack: list[ET.Element | str] = [el]
    while stack:
        node = stack.pop()
        if isinstance(node, str):
            parts.append(node)
            continue
        if node.tag in GHOST:
            continue
        if node.tag in (f"{SF}br", f"{SF}lnbr"):
            parts.append(" ")
        elif node.tag == f"{SF}tab":
            parts.append("\t")
        parts.append(node.text or "")
        for child in reversed(node):
            stack += [child.tail or "", child]
    return "".join(parts).replace("\ufffc", "").strip()


def _paras(el: ET.Element) -> Iterator[ET.Element]:
    """Paragraphs and tables in reading order, leaving a table's cells inside it"""
    stack = list(reversed(el))
    while stack:
        child = stack.pop()
        if child.tag in FURNITURE or child.tag in GHOST:
            continue
        if child.tag in (PARA, TABLE):
            yield child
        else:
            stack += reversed(child)


def _xml_table(model: ET.Element) -> dict[tuple[int, int], str]:
    # Cells sit flat in the datasource in row-major order, and the grid's width places them
    grid = next(model.iter(f"{SF}grid"), None)
    source = next(grid.iter(f"{SF}datasource"), None) if grid is not None else None
    cols = int(grid.get(f"{SF}numcols") or 0) if grid is not None else 0
    cells: dict[tuple[int, int], str] = {}
    for i, cell in enumerate(source if source is not None and cols else []):
        shown = next(cell.iter(f"{SF}ct"), None)
        if shown is not None:
            value = shown.get(f"{SFA}s") or "".join(shown.itertext())
        else:
            value = cell.get(f"{SF}v", "") if cell.tag == f"{SF}n" else ""
        if value := escape_cell(value.strip()):
            cells[(i // cols + 1, i % cols + 1)] = value
    return cells


def read_legacy(bundle: Bundle, name: str, src: Src, out: Converted) -> None:
    data = bundle.read(name)
    if name.endswith(".gz"):
        inflate = zlib.decompressobj(wbits=31)
        data = inflate.decompress(data, MAX_MEMBER_BYTES)
        if inflate.unconsumed_tail:
            raise ValueError(f"{name} expands past the {human_bytes(MAX_MEMBER_BYTES)} limit")
    root = parse(data)
    if name.startswith("index.apxl"):
        slides = [s for lst in root.iter(f"{KEY}slide-list") for s in lst if s.tag == f"{KEY}slide"]
        for n, slide in enumerate(slides, start=1):
            at = replace(src, slide=n)
            notes = [p for el in slide.iter(f"{KEY}notes") for p in el.iter(PARA)]
            skip = set(map(id, notes))
            found = [el for el in _paras(slide) if id(el) not in skip]
            shown = [t for p in found if p.tag == PARA and (t := _xml_text(p))]
            spoken = [t for p in notes if (t := _xml_text(p))]
            if body := slide_text(shown, spoken):
                out.blocks.append(Block(at, body))
            tables = [cells for el in found if el.tag == TABLE and (cells := _xml_table(el))]
            out.blocks += [Block(at, grid(cells)) for cells in tables]
        return
    body = next((el for el in root if el.tag == STORAGE and el.get(f"{SF}kind") == "body"), None)
    flow = None if body is None else body.find(f"{SF}text-body")
    if flow is None:
        # A layout document has no body flow, so its text is read in document order
        _flow(src, out, list(_paras(root)), {}, [])
        return
    # Text boxes outside the flow continue the count after the body, one number for each
    # paragraph with text as in newer Pages
    floating = [
        el
        for group in root.iterfind(f"{SL}drawables")
        for kind in group
        if kind.tag != f"{SL}masters-group"
        for el in _paras(kind)
        if el.tag == TABLE or _xml_text(el)
    ]
    attached = {a.get(f"{SFA}ID"): a for a in body.iter(ATTACHMENT)}
    _flow(src, out, list(_paras(flow)), attached, floating)


def _flow(
    src: Src,
    out: Converted,
    found: list[ET.Element],
    attached: dict[str | None, ET.Element],
    after: list[ET.Element],
) -> None:
    """Paragraphs cited by their place in the flow, empty ones included

    A table takes a number in the count, like the paragraph it stands in for, and one
    anchored in a paragraph shares its number. Elements in `after` continue the count
    """
    width = len(str(len(found)))
    lines: list[tuple[int, str]] = []

    def table(n: int, model: ET.Element) -> None:
        if cells := _xml_table(model):
            out.blocks += numbered_lines(lines, src, width)
            lines.clear()
            out.blocks.append(Block(replace(src, line=n), grid(cells)))

    for n, el in enumerate([*found, *after], start=1):
        if el.tag == TABLE:
            table(n, el)
            continue
        if text := _xml_text(el):
            lines.append((n, text))
        for ref in el.iter(ATTACHMENT_REF):
            target = attached.get(ref.get(f"{SFA}IDREF"))
            model = None if target is None else next(target.iter(TABLE), None)
            if model is not None:
                table(n, model)
            elif target is not None:
                lines += [(n, t) for p in target.iter(PARA) if (t := _xml_text(p))]
    out.blocks += numbered_lines(lines, src, width)
