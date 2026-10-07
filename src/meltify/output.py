from __future__ import annotations

import json
import sys
import unicodedata
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, TextIO

from meltify.evidence import Envelope

CELL_MAX = 80


def _cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, list | tuple):
        v = ", ".join(str(x) for x in v)
    elif isinstance(v, dict):
        v = json.dumps(v, ensure_ascii=False)
    s = str(v).replace("\n", " ")
    return s if len(s) <= CELL_MAX else s[: CELL_MAX - 1] + "…"


def _width(s: str) -> int:
    # Wide and full-width characters like Hangul take two terminal columns
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def table(rows: Sequence[dict[str, Any]], columns: Sequence[str]) -> str:
    cells = [[_cell(r.get(c)) for c in columns] for r in rows]
    widths = [max([_width(c)] + [_width(row[i]) for row in cells]) for i, c in enumerate(columns)]

    def line(values: Sequence[str]) -> str:
        padded = (v + " " * (w - _width(v)) for v, w in zip(values, widths, strict=True))
        return "  ".join(padded).rstrip()

    return "\n".join([line(columns), *(line(row) for row in cells)])


def append_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def emit(
    env: Envelope,
    *,
    as_json: bool,
    columns: Sequence[str],
    limit: int,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> None:
    # Resolve per call, so redirected streams in tests and wrappers are honored
    out = out or sys.stdout
    err = err or sys.stderr
    if as_json:
        json.dump(env.to_dict(), out, ensure_ascii=False, indent=2)
        out.write("\n")
        return
    rows = env.results
    if rows:
        shown = rows[:limit] if limit > 0 else rows
        out.write(table(shown, columns) + "\n")
        if len(rows) > len(shown):
            # Keep the agent's context small and point to the full list instead
            full = next((a["path"] for a in env.artifacts if a["role"] == "results"), None)
            hint = f", see {full}" if full else ", rerun with --limit 0 or --json"
            out.write(f"... {len(rows) - len(shown)} more rows{hint}\n")
    for a in env.artifacts:
        out.write(f"artifact {a['role']}: {a['path']}\n")
    if env.summary:
        out.write(env.summary + "\n")
    for w in env.warnings:
        err.write(f"warning: {w}\n")
    for e in env.errors:
        err.write(f"error {e['code']}: {e['message']}\n")
        if e.get("hint"):
            err.write(f"  hint: {e['hint']}\n")
