"""Read the object graph of an iWork 2013+ document without Apple's schemas

Pages, Numbers and Keynote keep their content in `Index/*.iwa` files. Each is a
run of chunks, a zero tag byte and a 3-byte little-endian length followed by a raw
Snappy block with no stream framing or CRC. The joined blocks are a stream of
archives, each a varint length, a `TSP.ArchiveInfo` naming the object id and the
message types, then the payloads. Fields are read by number, so only the message
and field numbers in iwork.py are format knowledge. Ported from Docling's pure
Python reader (MIT), https://github.com/docling-project/docling/pull/4062
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import NamedTuple

# A hostile chunk can declare 16 MB and expand 21x, so every stream of a document
# shares one cap
MAX_STREAM_BYTES = 256 << 20
# A TSP.Reference is a single varint field, anything longer is a nested message
MAX_REFERENCE = 11

Fields = dict[int, list[int | bytes]]


class IwaError(ValueError):
    pass


class Obj(NamedTuple):
    id: int
    type: int
    payload: bytes


def _varint(buf: bytes, pos: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        if pos >= len(buf) or shift > 63:
            raise IwaError("bad varint")
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7


def fields(buf: bytes) -> Fields:
    """Decode a protobuf message into {field number: [values]}

    Length-delimited values stay bytes, so the caller decides whether one is a
    string, a nested message or a reference. An unreadable message decodes as
    empty, since one odd sub-message shouldn't sink the document
    """
    out: Fields = {}
    pos = 0
    try:
        while pos < len(buf):
            key, pos = _varint(buf, pos)
            wire = key & 7
            value: int | bytes
            if wire == 0:
                value, pos = _varint(buf, pos)
            elif wire == 2:
                size, pos = _varint(buf, pos)
                if pos + size > len(buf):
                    raise IwaError("truncated field")
                value, pos = buf[pos : pos + size], pos + size
            elif wire == 1:
                value, pos = buf[pos : pos + 8], pos + 8
            elif wire == 5:
                value, pos = buf[pos : pos + 4], pos + 4
            else:
                raise IwaError(f"wire type {wire}")
            out.setdefault(key >> 3, []).append(value)
    except IwaError:
        return {}
    return out


def first(f: Fields, n: int) -> int | bytes | None:
    return f.get(n, [None])[0]


def text(f: Fields, n: int) -> str:
    value = first(f, n)
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else ""


def ref(f: Fields, n: int) -> int | None:
    value = first(f, n)
    return _target(value) if isinstance(value, bytes) else None


def refs(f: Fields, n: int) -> list[int]:
    return [t for v in f.get(n, []) if isinstance(v, bytes) and (t := _target(v)) is not None]


def _target(value: bytes) -> int | None:
    if len(value) > MAX_REFERENCE:
        return None
    f = fields(value)
    target = first(f, 1)
    return target if len(f) == 1 and isinstance(target, int) else None


def all_refs(payload: bytes, depth: int = 0) -> list[int]:
    """Every object id a message points at, nested messages included, in stored order"""
    found: list[int] = []
    if depth > 4:
        return found
    for values in fields(payload).values():
        for v in values:
            if not isinstance(v, bytes):
                continue
            target = _target(v)
            if target is not None:
                found.append(target)
            else:
                found += all_refs(v, depth + 1)
    return found


def unsnappy(block: bytes, limit: int) -> bytes:
    """One raw Snappy block. Only the decoder half is needed, and it's small"""
    expected, pos = _varint(block, 0)
    if expected > limit:
        raise IwaError(f"snappy block of {expected} bytes is over the limit")
    out = bytearray()
    size = len(block)
    while pos < size:
        tag = block[pos]
        pos += 1
        kind = tag & 3
        if kind == 0:
            length = tag >> 2
            if length >= 60:
                extra = length - 59
                length = int.from_bytes(block[pos : pos + extra], "little")
                pos += extra
            length += 1
            if pos + length > size:
                raise IwaError("truncated snappy literal")
            out += block[pos : pos + length]
            pos += length
        else:
            at = pos - 1
            if kind == 1:
                length = 4 + ((tag >> 2) & 7)
                offset = ((tag >> 5) << 8) | block[pos] if pos < size else 0
                pos += 1
            else:
                width = 2 if kind == 2 else 4
                length = (tag >> 2) + 1
                offset = int.from_bytes(block[pos : pos + width], "little")
                pos += width
            if offset == 0 or offset > len(out):
                raise IwaError("snappy copy outside the window")
            pos, copies = _repeats(block, pos, block[at:pos])
            length *= copies
            if len(out) + length > expected:
                raise IwaError("snappy block overran its declared size")
            start = len(out) - offset
            if offset >= length:
                out += out[start : start + length]
            else:
                # An overlapping copy repeats the last `offset` bytes, so the run is tiled
                # in one step instead of growing a byte at a time
                out += (out[start:] * (length // offset + 1))[:length]
        if len(out) > expected:
            raise IwaError("snappy block overran its declared size")
    if len(out) != expected:
        raise IwaError("snappy block came up short")
    return bytes(out)


def _repeats(block: bytes, pos: int, tag: bytes) -> tuple[int, int]:
    """Where a run of the same copy element ends, and how many copies it makes

    Copies at one offset keep the output periodic, so a run of the same copy is one longer
    copy. Probes double and then halve, so a long run costs a few compares instead of a
    pass through the decoder loop for each copy
    """
    step, copies = 1, 1
    while block.startswith(tag * step, pos):
        pos += len(tag) * step
        copies += step
        step *= 2
    while step > 1:
        step //= 2
        if block.startswith(tag * step, pos):
            pos += len(tag) * step
            copies += step
    return pos, copies


def is_iwa(data: bytes) -> bool:
    pos = 0
    while pos < len(data):
        if data[pos] != 0 or pos + 4 > len(data):
            return False
        pos += 4 + int.from_bytes(data[pos + 1 : pos + 4], "little")
    return pos == len(data)


def _stream(data: bytes, limit: int) -> bytes:
    out = bytearray()
    pos = 0
    while pos < len(data):
        if data[pos] != 0 or pos + 4 > len(data):
            raise IwaError("not an IWA chunk")
        length = int.from_bytes(data[pos + 1 : pos + 4], "little")
        block = data[pos + 4 : pos + 4 + length]
        if len(block) != length:
            raise IwaError("truncated IWA chunk")
        pos += 4 + length
        out += unsnappy(block, limit - len(out))
    return bytes(out)


def objects(files: Iterable[bytes]) -> dict[int, Obj]:
    """Every archived object across the given .iwa files, by id

    An object can carry several messages, and the first one is the object itself. The
    files decompress within MAX_STREAM_BYTES together, so many files can't multiply it
    """
    found: dict[int, Obj] = {}
    left = MAX_STREAM_BYTES
    for data in files:
        stream = _stream(data, left)
        left -= len(stream)
        pos = 0
        while pos < len(stream):
            size, pos = _varint(stream, pos)
            info = fields(stream[pos : pos + size])
            pos += size
            oid = first(info, 1)
            for n, message in enumerate(info.get(2, [])):
                if not isinstance(message, bytes):
                    continue
                m = fields(message)
                kind, length = first(m, 1), first(m, 3)
                if not isinstance(kind, int) or not isinstance(length, int):
                    raise IwaError("bad message info")
                if n == 0 and isinstance(oid, int):
                    found[oid] = Obj(oid, kind, stream[pos : pos + length])
                pos += length
    return found
