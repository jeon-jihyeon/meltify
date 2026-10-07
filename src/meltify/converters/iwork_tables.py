"""Cells of iWork tables, packed in IWA tiles or read through numbers-parser"""

from __future__ import annotations

import datetime as dt
import struct
import tempfile
import warnings
import zipfile
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from meltify.converters import Block, Converted, iwa
from meltify.converters.iwork_bundle import Bundle
from meltify.converters.tables import escape_cell, lettered
from meltify.evidence import Src

# Message types for tables and the text storage their rich-text cells point to, which
# iwork's drawable reader uses as well
TSWP_STORAGE = 2001
TST_INFO, TST_MODEL, TST_TILE, TST_LIST, TST_TEXT_REF = 6000, 6001, 6002, 6005, 6218
# Numbers counts dates in seconds from the start of 2001
EPOCH = dt.datetime(2001, 1, 1)


def _cell_values(store: iwa.Fields, objs: dict[int, iwa.Obj]) -> tuple[dict, dict]:
    """Shared strings and rich text, keyed the way packed cells reference them"""

    def read_list(n: int, decode: Callable[[iwa.Fields], str | None]) -> dict[int, str]:
        root = objs.get(iwa.ref(store, n) or -1)
        if root is None or root.type != TST_LIST:
            return {}
        payloads = [root.payload]
        payloads += [objs[s].payload for s in iwa.refs(iwa.fields(root.payload), 4) if s in objs]
        values = {}
        for payload in payloads:
            for entry in iwa.fields(payload).get(3, []):
                if isinstance(entry, bytes):
                    f = iwa.fields(entry)
                    key, value = iwa.first(f, 1), decode(f)
                    if isinstance(key, int) and value is not None:
                        values[key] = value
        return values

    def plain(f: iwa.Fields) -> str | None:
        value = iwa.first(f, 3)
        return value.decode("utf-8", "replace") if isinstance(value, bytes) else None

    def rich(f: iwa.Fields) -> str | None:
        link = objs.get(iwa.ref(f, 9) or -1)
        if link is None or link.type != TST_TEXT_REF:
            return None
        storage = objs.get(iwa.ref(iwa.fields(link.payload), 1) or -1)
        if storage is None or storage.type != TSWP_STORAGE:
            return None
        return iwa.text(iwa.fields(storage.payload), 3)

    return read_list(4, plain), read_list(17, rich)


def _decimal128(b: bytes) -> Decimal | None:
    if b[15] & 0x78 == 0x78:
        return None
    exponent = (((b[15] & 0x7F) << 7) | (b[14] >> 1)) - 6176
    coefficient = int.from_bytes(b[:14], "little") | ((b[14] & 1) << 112)
    value = Decimal(coefficient).scaleb(exponent)
    return -value if b[15] & 0x80 else value


def _number(value: Decimal | float, kind: int) -> str:
    if kind == 5:
        when = EPOCH + dt.timedelta(seconds=float(value))
        return when.date().isoformat() if when.time() == dt.time() else when.isoformat()
    if kind == 6:
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        value = Decimal(repr(value))
    return format(value.normalize(), "f")


def _cell(buf: bytes, at: int, strings: dict, rich: dict) -> str | None:
    """One packed TST cell, in the version 5 layout since 2017 or the older version 4

    Version 4 doesn't describe its own layout, so it is read the way real files
    write it and anything that doesn't fit yields nothing rather than misread bytes
    """
    if at < 0 or at + 12 > len(buf):
        return None
    version, kind = buf[at], buf[at + 1]
    if version == 5:
        flags = int.from_bytes(buf[at + 8 : at + 12], "little")
        pos = at + 12
        number: Decimal | float | None = None
        found: str | None = None
        for flag, width in ((1, 16), (2, 8), (4, 8), (8, 4), (16, 4)):
            if not flags & flag:
                continue
            if pos + width > len(buf):
                return None
            chunk = buf[pos : pos + width]
            if flag == 1:
                number = _decimal128(chunk)
            elif flag in (2, 4):
                number = struct.unpack("<d", chunk)[0]
            else:
                key = int.from_bytes(chunk, "little")
                found = (strings if flag == 8 else rich).get(key, found)
            pos += width
        if found is not None:
            return found
        return _number(number, kind) if number is not None else None
    if version == 4:
        flags = int.from_bytes(buf[at + 4 : at + 8], "little")
        ids = bin(flags).count("1")
        numeric = kind in (2, 5, 6, 7, 10)
        end = at + 12 + 4 * ids + (8 if numeric else 0)
        if not ids or end > len(buf):
            return None
        if numeric:
            return _number(struct.unpack("<d", buf[end - 12 : end - 4])[0], kind)
        if kind == 3:
            return strings.get(int.from_bytes(buf[end - 4 : end], "little"))
    return None


def table_cells(model: iwa.Obj, objs: dict[int, iwa.Obj]) -> dict[tuple[int, int], str]:
    f = iwa.fields(model.payload)
    rows, cols, store_raw = iwa.first(f, 6), iwa.first(f, 7), iwa.first(f, 4)
    if not isinstance(rows, int) or not isinstance(cols, int) or not isinstance(store_raw, bytes):
        return {}
    store = iwa.fields(store_raw)
    strings, rich = _cell_values(store, objs)
    tiles = iwa.first(store, 3)
    cells: dict[tuple[int, int], str] = {}
    if not isinstance(tiles, bytes):
        return cells
    for entry in iwa.fields(tiles).get(1, []):
        if not isinstance(entry, bytes):
            continue
        e = iwa.fields(entry)
        tile = objs.get(iwa.ref(e, 2) or -1)
        first_row = iwa.first(e, 1)
        if tile is None or tile.type != TST_TILE:
            continue
        base = first_row if isinstance(first_row, int) else 0
        for row_raw in iwa.fields(tile.payload).get(5, []):
            if not isinstance(row_raw, bytes):
                continue
            row = iwa.fields(row_raw)
            r = iwa.first(row, 1)
            # Pages 5.2 added wider offsets scaled by 4 and kept the old pair for older apps
            buf, offsets = iwa.first(row, 6), iwa.first(row, 7)
            scale = 4 if iwa.first(row, 8) else 1
            if not isinstance(buf, bytes) or not isinstance(offsets, bytes):
                buf, offsets, scale = iwa.first(row, 3), iwa.first(row, 4), 1
            if (
                not isinstance(r, int)
                or not isinstance(buf, bytes)
                or not isinstance(offsets, bytes)
            ):
                continue
            for c in range(min(len(offsets) // 2, cols)):
                start = int.from_bytes(offsets[2 * c : 2 * c + 2], "little", signed=True)
                if start < 0 or base + r >= rows:
                    continue
                value = _cell(buf, start * scale, strings, rich)
                if value and (value := escape_cell(value.strip())):
                    cells[(base + r + 1, c + 1)] = value
    return cells


def _cell_text(cell: Any) -> str:
    from numbers_parser import MergedCell

    # A merged range shows its value once, at the top-left cell
    if isinstance(cell, MergedCell):
        return ""
    return escape_cell(cell.formatted_value or "")


def read_numbers(path: Path, src: Src, out: Converted) -> None:
    from numbers_parser import Document

    # numbers-parser warns about every document version it hasn't seen, which isn't actionable
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        doc = Document(path)
    for sheet in doc.sheets:
        for table in sheet.tables:
            rows = [
                (n, [_cell_text(c) for c in row])
                for n, row in enumerate(table.iter_rows(), start=1)
            ]
            text = lettered((n, r) for n, r in rows if any(r))
            if text is not None:
                table_src = replace(src, sheet=f"{sheet.name}>{table.name}")
                out.blocks.append(Block(table_src, text))


def read_numbers_unlocked(bundle: Bundle, src: Src, out: Converted) -> None:
    # numbers-parser reads a plain package, so the decrypted files go to a private temp zip
    with tempfile.TemporaryDirectory() as tmp:
        plain = Path(tmp) / "unlocked.numbers"
        with zipfile.ZipFile(plain, "w", zipfile.ZIP_DEFLATED) as z:
            for name in bundle.names:
                if name not in (".iwph", ".iwpv2"):
                    z.writestr(name, bundle.read(name))
        read_numbers(plain, src, out)
