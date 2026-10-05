from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Any

from meltify.converters import Block, Converted
from meltify.converters.office import CHART, NS, R_ID, Embeds, image_ids, rels, xml
from meltify.evidence import Src

ANCHORS = ("twoCellAnchor", "oneCellAnchor", "absoluteAnchor")


def _cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.isoformat(sep=" ", timespec="minutes").removesuffix(" 00:00")
    if isinstance(v, date):
        return v.isoformat()
    return str(v).replace("|", "\\|").replace("\n", " ")


def _anchor_cell(anchor: Any) -> str | None:
    from openpyxl.utils import get_column_letter

    start = anchor.find("xdr:from", NS)
    if start is None:
        return None
    col, row = start.findtext("xdr:col", None, NS), start.findtext("xdr:row", None, NS)
    if col is None or row is None:
        return None
    return f"{get_column_letter(int(col) + 1)}{int(row) + 1}"


def images(path: Path, src: Src) -> Embeds:
    """Pictures in each sheet's drawing, cited by the cell their top left corner sits on"""
    import zipfile

    embeds = Embeds()
    with zipfile.ZipFile(path) as z:
        book = "xl/workbook.xml"
        parts = rels(z, book)
        sheets = xml(z, book).find("s:sheets", NS)
        for sheet in [] if sheets is None else sheets:
            part = parts.get(sheet.get(R_ID) or "")
            if part is None or part not in z.namelist():
                continue
            name = sheet.get("name") or ""
            drawings = [
                t
                for t in rels(z, part).values()
                if t and t.startswith("xl/drawings/") and t.endswith(".xml")
            ]
            n = 0
            for drawing in drawings:
                if drawing not in z.namelist():
                    continue
                root = xml(z, drawing)
                targets = rels(z, drawing)
                for anchor in root:
                    if anchor.tag.rsplit("}", 1)[-1] not in ANCHORS:
                        continue
                    if charts := sum(1 for _ in anchor.iter(CHART)):
                        embeds.skipped["chart"] += charts
                    at = replace(src, sheet=name, cell=_anchor_cell(anchor))
                    for rid in image_ids(anchor):
                        n += 1
                        embeds.member(z, replace(at, img=n), targets, rid)
    return embeds


def convert(path: Path, src: Src) -> Converted:
    from openpyxl import load_workbook
    from openpyxl.utils import get_column_letter

    out = Converted("sheet")
    wb = load_workbook(path, read_only=True, data_only=True)
    hidden_sheets = []
    try:
        for ws in wb.worksheets:
            if ws.sheet_state != "visible":
                hidden_sheets.append(ws.title)
            # Read-only mode yields placeholders without coordinates, so count rows from 1
            rows = [
                (n, r)
                for n, r in enumerate(ws.iter_rows(min_row=1, values_only=True), start=1)
                if any(v is not None for v in r)
            ]
            if not rows:
                continue
            width = max(len(r) for _, r in rows)
            letters = [get_column_letter(i + 1) for i in range(width)]
            # Row numbers and column letters let a reader cite a single cell
            lines = ["| row | " + " | ".join(letters) + " |", "|---" * (width + 1) + "|"]
            for n, r in rows:
                values = [_cell(v) for v in r] + [""] * (width - len(r))
                lines.append(f"| {n} | " + " | ".join(values) + " |")
            out.blocks.append(Block(replace(src, sheet=ws.title), "\n".join(lines)))
    finally:
        wb.close()
    if hidden_sheets:
        out.needs.append("hidden sheets " + ", ".join(hidden_sheets))
    images(path, src).into(out)
    return out
