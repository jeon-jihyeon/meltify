"""Markdown tables whose rows and columns a reader can cite cell by cell"""

from __future__ import annotations

import json
from collections.abc import Iterable

# A database or data file cell longer than this is cut, since a sample only shows its shape
MAX_SAMPLE_CELL = 200
# Rows a data file's sample shows
SAMPLE_ROWS = 20


def escape_cell(text: str) -> str:
    """Text that stays in its cell, with pipes escaped and line breaks flattened"""
    return text.replace("|", "\\|").replace("\n", " ")


def sample_cell(value: object) -> str:
    """A value from a sampled database or data file row, cut to MAX_SAMPLE_CELL"""
    if value is None:
        return "NULL"
    if isinstance(value, bytes):
        return f"<blob {len(value):,} bytes>"
    text = escape_cell(str(value))
    return text if len(text) <= MAX_SAMPLE_CELL else text[:MAX_SAMPLE_CELL] + "..."


def value_cell(value: object) -> str:
    """A sampled value where NULL means missing, so a string reading NULL itself is quoted

    Booleans and nested values show as JSON, the way a JSON or Arrow source holds them
    """
    if value == "NULL":
        return '"NULL"'
    if isinstance(value, bool | dict | list):
        value = json.dumps(value, ensure_ascii=False, default=str)
    return sample_cell(value)


def counted(n: int, noun: str) -> str:
    """`1 row`, `2 rows`, `1,000 rows`"""
    return f"{n:,} {noun}" if n == 1 else f"{n:,} {noun}s"


def sampled(name: str, columns: list[str], rows: list, total: int) -> str:
    """A data file's sample: `{name}: first N of M rows`, a `columns|` line, then `row=K|` lines"""
    head = counted(total, "row")
    if total > len(rows):
        head = f"first {len(rows)} of {head}"
    lines = [f"{name}: {head}", "columns| " + " | ".join(sample_cell(c) for c in columns)]
    lines += [f"row={n}| " + " | ".join(map(value_cell, r)) for n, r in enumerate(rows, start=1)]
    return "\n".join(lines)


def lettered(rows: Iterable[tuple[int, list[str]]]) -> str | None:
    """Rows headed by column letters and led by their row number, None without any rows

    Row numbers and column letters let a reader cite a single cell. Short rows are padded
    to the widest one. Each row is joined as it comes, which holds far less than its cells
    """
    from openpyxl.utils import get_column_letter

    kept = [(f"| {n} | " + " | ".join(cells), len(cells)) for n, cells in rows]
    if not kept:
        return None
    width = max(k for _, k in kept)
    letters = [get_column_letter(i + 1) for i in range(width)]
    lines = ["| row | " + " | ".join(letters) + " |", "|---" * (width + 1) + "|"]
    lines += [line + " | " * (width - k) + " |" for line, k in kept]
    return "\n".join(lines)


def grid(cells: dict[tuple[int, int], str]) -> str:
    """Non-empty cells keyed by 1-based row and column as a lettered table"""
    width = max(c for _, c in cells)
    rows = sorted({r for r, _ in cells})
    return lettered((r, [cells.get((r, c), "") for c in range(1, width + 1)]) for r in rows) or ""
