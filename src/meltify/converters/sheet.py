from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Any

from meltify.converters import Block, Converted
from meltify.evidence import Src


def _cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.isoformat(sep=" ", timespec="minutes").removesuffix(" 00:00")
    if isinstance(v, date):
        return v.isoformat()
    return str(v).replace("|", "\\|").replace("\n", " ")


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
    return out
