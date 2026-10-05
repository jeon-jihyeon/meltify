from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

from meltify.converters import Block, Converted
from meltify.evidence import Src

SAMPLE_ROWS = 20
MAX_CELL = 200


def _cell(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bytes):
        return f"<blob {len(value):,} bytes>"
    text = str(value).replace("\n", " ").replace("|", "\\|")
    return text if len(text) <= MAX_CELL else text[:MAX_CELL] + "..."


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _table(db: sqlite3.Connection, name: str, src: Src) -> Block:
    q = _quote(name)
    total = db.execute(f"select count(*) from {q}").fetchone()[0]
    try:
        cur = db.execute(f"select rowid, * from {q} limit {SAMPLE_ROWS}")
        keyed = True
    except sqlite3.OperationalError:
        # WITHOUT ROWID tables and some virtual tables have no rowid to cite
        cur = db.execute(f"select * from {q} limit {SAMPLE_ROWS}")
        keyed = False
    columns = [d[0] for d in cur.description][1 if keyed else 0 :]
    rows = cur.fetchall()
    shown = f"first {len(rows)} of {total:,} rows" if total > len(rows) else f"{total:,} rows"
    lines = [f"{name}: {shown}", "columns| " + " | ".join(columns)]
    for n, row in enumerate(rows, start=1):
        label = f"rowid={row[0]}" if keyed else f"row {n}"
        values = row[1:] if keyed else row
        lines.append(f"{label}| " + " | ".join(_cell(v) for v in values))
    first = f"rowid={rows[0][0]}" if keyed and rows else None
    return Block(replace(src, sheet=name, cell=first), "\n".join(lines))


def convert(path: Path, src: Src) -> Converted:
    # immutable=1 skips locking and journal files, so a database in use or on a
    # read-only disk opens without being touched
    uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
    out = Converted("sqlite")
    try:
        db = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as e:
        out.needs.append(f"unreadable database: {e}")
        return out
    try:
        try:
            schema = db.execute(
                "select type, name, sql from sqlite_master where sql is not null order by rowid"
            ).fetchall()
        except sqlite3.DatabaseError as e:
            out.needs.append(f"unreadable database: {e}")
            return out
        out.blocks.append(Block(src, "\n".join(f"{sql};" for _, _, sql in schema) or "(empty)"))
        for kind, name, _ in schema:
            if kind != "table" or name.startswith("sqlite_"):
                continue
            try:
                out.blocks.append(_table(db, name, src))
            except sqlite3.Error as e:
                out.needs.append(f"unreadable table {name}: {e}")
    finally:
        db.close()
    return out
