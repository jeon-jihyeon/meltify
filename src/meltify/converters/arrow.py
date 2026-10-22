"""Arrow IPC files and streams: schema metadata, a summary over every row, then sampled rows

Files are memory-mapped, so uncompressed batches stay views into the file and the summary
runs in pyarrow.compute without turning rows into Python. A cite like `a.arrows:3` points at the
third row, the way Parquet cites its rows
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from meltify.converters import Block, Converted
from meltify.converters.limits import MAX_MEMBER_BYTES, human_bytes
from meltify.converters.tables import SAMPLE_ROWS, counted, sample_cell, sampled, value_cell
from meltify.evidence import Src
from meltify.safe import MissingTool

MAX_META_CHARS = 2000
FILE_MAGIC = b"ARROW1"
FEATHER_V1 = b"FEA1"
CONTINUATION = b"\xff\xff\xff\xff"
HINT = "meltify doctor --install parquet"
# Flatbuffer header types of the IPC Message table
DICTIONARY_BATCH = 2
RECORD_BATCH = 3
# What count_distinct's hash table may take, estimated per row and per column byte. Measured
# on pyarrow 25 at 216 bytes a row for unique int64, 328 for unique 12 char strings and 2862
# for unique 1000 char strings, so the estimate stays above each
MAX_DISTINCT_BYTES = MAX_MEMBER_BYTES
DISTINCT_ROW_BYTES = 512
DISTINCT_BYTE_FACTOR = 4


def sniff(path: Path, head: bytes) -> bool:
    if head.startswith(FILE_MAGIC):
        return True
    # A stream opens with the continuation marker and a metadata length padded to 8 bytes
    if head.startswith(CONTINUATION) and len(head) >= 8:
        size = int.from_bytes(head[4:8], "little")
        return size > 0 and size % 8 == 0
    return False


def require():
    """pyarrow itself, or MissingTool naming the extra that installs it"""
    try:
        import pyarrow
    except ImportError as e:
        raise MissingTool("pyarrow", HINT) from e
    return pyarrow


@dataclass
class Loaded:
    table: Any
    needs: list[str]
    # Bytes compressed buffers unpacked into, the only ones not left in the memory map
    unpacked: int = 0
    # What the batches within the limit declare, read or not, so it matches `unpacks`
    declared: int = 0


def _field(meta: bytes, table: int, i: int) -> int | None:
    """Where field `i` of the flatbuffer table at `table` sits, None when it's absent"""
    vtable = table - struct.unpack_from("<i", meta, table)[0]
    if 6 + 2 * i > struct.unpack_from("<H", meta, vtable)[0]:
        return None
    offset = struct.unpack_from("<H", meta, vtable + 4 + 2 * i)[0]
    return table + offset if offset else None


def _ref(meta: bytes, at: int) -> int:
    return at + struct.unpack_from("<I", meta, at)[0]


def _declared(message) -> int:
    """Bytes a dictionary or record batch message says its compressed buffers unpack to

    Each compressed buffer starts with its unpacked length, and Arrow refuses one that
    unpacks to anything else, so the sum bounds the batch before it's decoded
    """
    meta = message.metadata.to_pybytes()
    body = memoryview(message.body) if message.body is not None else memoryview(b"")
    root = _ref(meta, 0)
    kind, header = _field(meta, root, 1), _field(meta, root, 2)
    if kind is None or header is None or meta[kind] not in (DICTIONARY_BATCH, RECORD_BATCH):
        return 0
    batch = _ref(meta, header)
    if meta[kind] == DICTIONARY_BATCH:
        if (data := _field(meta, batch, 1)) is None:
            return 0
        batch = _ref(meta, data)
    buffers = _field(meta, batch, 2)
    # Uncompressed buffers stay views into the memory map
    if buffers is None or _field(meta, batch, 3) is None:
        return 0
    vector = _ref(meta, buffers)
    total = 0
    for k in range(struct.unpack_from("<I", meta, vector)[0]):
        offset, length = struct.unpack_from("<qq", meta, vector + 4 + 16 * k)
        if length >= 8 and 0 <= offset <= len(body) - 8:
            total += max(struct.unpack_from("<q", body, offset)[0], 0)
    return total


def _costs(path: Path, start: int, pa) -> tuple[list[int], Exception | None]:
    """Bytes each record batch unpacks to, its dictionaries included, and why the scan stopped

    Only message headers and the length in front of each buffer are read, nothing decoded
    """
    import pyarrow.ipc as ipc

    costs: list[int] = []
    pending = 0
    with pa.memory_map(str(path)) as scan:
        scan.seek(start)
        try:
            reader = ipc.MessageReader.open_stream(scan)
            while True:
                message = reader.read_next_message()
                try:
                    size = _declared(message)
                except (struct.error, IndexError):
                    # A header this can't walk fails pyarrow's own check when it's decoded
                    size = 0
                if message.type == "dictionary":
                    pending += size
                elif message.type == "record batch":
                    costs.append(pending + size)
                    pending = 0
        except StopIteration:
            return costs, None
        except (pa.ArrowException, OSError) as e:
            return costs, e


def _start(path: Path) -> int:
    """Where the stream begins, after an IPC file's magic and padding"""
    with path.open("rb") as f:
        return 8 if f.read(len(FILE_MAGIC)) == FILE_MAGIC else 0


def _within(costs: list[int], limit: int) -> int:
    total = 0
    for cost in costs:
        if total + cost > limit:
            break
        total += cost
    return total


def unpacks(path: Path, limit: int = MAX_MEMBER_BYTES) -> int:
    """What `load` declares for the same `limit`, from the message headers alone"""
    return _within(_costs(path, _start(path), require())[0], limit)


def _ended(count: int, e: Exception) -> str:
    if not count:
        return f"stream ended before its first whole batch: {e}"[:200]
    return f"stream ended early after {count} {'batch' if count == 1 else 'batches'}: {e}"[:200]


def load(path: Path, limit: int = MAX_MEMBER_BYTES, limit_name: str | None = None) -> Loaded:
    """The table a file or stream holds, its uncompressed buffers still in the memory map

    An IPC file is read as the stream after its magic, so every batch passes through the
    same size check. Compressed batches unpack on the heap, and the batch that would take
    them past `limit` and every later one aren't read, the way Parquet checks its row groups.
    `limit_name` names that limit in the need when it isn't the plain per-file one
    """
    pa = require()
    import pyarrow.ipc as ipc

    with path.open("rb") as f:
        head = f.read(8)
        f.seek(max(path.stat().st_size - len(FILE_MAGIC), 0))
        tail = f.read()
    if head.startswith(FEATHER_V1):
        raise ValueError("Feather v1 isn't read, rewrite it as Feather v2 with a current pyarrow")
    out = Loaded(None, [])
    start = _start(path)
    if start and tail != FILE_MAGIC:
        out.needs.append("file footer missing, read as a stream")
    costs, broken = _costs(path, start, pa)
    out.declared = _within(costs, limit)
    # Closing the map drops only its file handle, since the table's buffers keep the mapping
    with pa.memory_map(str(path)) as source:
        source.seek(start)
        try:
            reader = ipc.open_stream(source)
        except (pa.ArrowException, OSError) as e:
            raise ValueError(f"not an Arrow IPC file or stream: {e}"[:200]) from e
        batches = []
        for n, cost in enumerate(costs, start=1):
            if out.unpacked + cost > limit:
                past = limit_name or human_bytes(limit)
                out.needs.append(f"batch {n} not read, it unpacks past {past}")
                broken = None
                break
            try:
                batch = reader.read_next_batch()
                # The IPC reader checks only the layout, and compute kernels trust offsets
                batch.validate(full=True)
            except StopIteration:
                break
            except (pa.ArrowException, OSError) as e:
                broken = e
                break
            out.unpacked += cost
            batches.append(batch)
        out.table = pa.Table.from_batches(batches, reader.schema)
    if broken is not None:
        out.needs.append(_ended(len(batches), broken))
    return out


def _type(t) -> str:
    return getattr(t, "extension_name", None) or str(t)


def _orderable(t, pa) -> bool:
    types = pa.types
    checks = [types.is_integer, types.is_floating, types.is_decimal, types.is_boolean]
    checks += [types.is_temporal, types.is_string, types.is_large_string, types.is_string_view]
    return any(check(t) for check in checks)


def _measure(pa, fn, *args) -> Any:
    try:
        return fn(*args)
    except (pa.ArrowNotImplementedError, pa.ArrowInvalid, pa.ArrowTypeError):
        # Not every Arrow type supports every kernel, and a blank cell says so
        return None


def _py(scalar, pa) -> Any:
    """A scalar as Python, or as Arrow's own text when Python can't hold it

    A valid timestamp or date may lie outside Python's datetime range, and `str` on the
    scalar goes through the same conversion, so Arrow's string cast spells it instead
    """
    try:
        return scalar.as_py()
    except (OverflowError, ValueError):
        try:
            return scalar.cast(pa.string()).as_py()
        except (pa.ArrowException, OverflowError, ValueError):
            return ""


def _pylist(column, pa) -> list[Any]:
    try:
        return column.to_pylist()
    except (OverflowError, ValueError):
        # One cell out of range shouldn't cost the whole sample
        return [_py(scalar, pa) for scalar in column]


def _distinct_fits(column) -> bool:
    """Whether count_distinct's hash table for `column` stays within MAX_DISTINCT_BYTES"""
    estimate = len(column) * DISTINCT_ROW_BYTES + column.nbytes * DISTINCT_BYTE_FACTOR
    return estimate <= MAX_DISTINCT_BYTES


def summary(table, name: str) -> str:
    """Nulls, min, max and distinct per column, computed over every row by pyarrow.compute"""
    pa = require()
    import pyarrow.compute as pc

    rows = counted(table.num_rows, "row")
    if table.num_rows > 1:
        rows = f"all {rows}"
    lines = [
        f"{name}: computed over {rows}",
        "| column | type | nulls | min | max | distinct |",
        "|---|---|---|---|---|---|",
    ]
    for field, column in zip(table.schema, table.columns, strict=True):
        low = high = distinct = None
        storage = column
        # Canonical types like arrow.json derive from the base class, not ExtensionType
        if isinstance(field.type, pa.BaseExtensionType):
            storage = pa.chunked_array([c.storage for c in column.chunks], field.type.storage_type)
        if _orderable(field.type, pa):
            pair = _measure(pa, pc.min_max, column)
            if pair is not None:
                low, high = _py(pair["min"], pa), _py(pair["max"], pa)
        if not _distinct_fits(storage):
            distinct = "not computed"
        elif (found := _measure(pa, pc.count_distinct, storage)) is not None:
            distinct = f"{found.as_py():,}"
        bounds = ["" if v is None else value_cell(v) for v in (low, high)]
        cells = [sample_cell(field.name), sample_cell(_type(field.type))]
        cells += [f"{column.null_count:,}", *bounds, distinct or ""]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def sample(table, name: str) -> str:
    """The first rows, the only ones turned into Python values"""
    pa = require()
    columns = [f"{f.name} ({_type(f.type)})" for f in table.schema]
    # Column by column, so two columns with the same name both survive
    values = [_pylist(c, pa) for c in table.slice(0, SAMPLE_ROWS).columns]
    return sampled(name, columns, list(zip(*values, strict=True)), table.num_rows)


def metadata(schema) -> str | None:
    if not schema.metadata:
        return None
    lines = []
    for key, value in schema.metadata.items():
        text = value.decode("utf-8", errors="replace").replace("\n", " ")
        if len(text) > MAX_META_CHARS:
            text = text[:MAX_META_CHARS] + "..."
        lines.append(f"{key.decode('utf-8', errors='replace')}: {text}")
    return "\n".join(lines)


def blocks(table, name: str, src: Src, rows: Src) -> list[Block]:
    """Metadata and summary at `src`, the sample at `rows`"""
    out = []
    if (meta := metadata(table.schema)) is not None:
        out.append(Block(src, meta, role="metadata"))
    out.append(Block(src, summary(table, name), role="summary"))
    out.append(Block(rows, sample(table, name), role="sample"))
    return out


def convert(path: Path, src: Src) -> Converted:
    out = Converted("arrow")
    loaded = load(path)
    rows = replace(src, line=1) if loaded.table.num_rows else src
    out.blocks = blocks(loaded.table, path.name, src, rows)
    out.needs += loaded.needs
    return out
