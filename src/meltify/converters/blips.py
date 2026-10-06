"""Office drawing records that Word and PowerPoint binaries share, and their picture store"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Iterator

from meltify.converters.embeds import OVER_TOTAL, Embeds
from meltify.converters.limits import MAX_PART_BYTES
from meltify.evidence import Src

PICTURE_ENTRY = 0xF007
SHAPE_OPTIONS = 0xF00B
# Shape properties that point at a picture in the store
PICTURE_PROPS = {0x0104, 0x0186}  # pib, fillBlip
PIB_NAME = 0x0105  # the file a linked picture points at
# Picture record types, mapped to the extension Embeds expects
BLIPS = {
    0xF01A: ".emf",
    0xF01B: ".wmf",
    0xF01C: ".pict",
    0xF01D: ".jpg",
    0xF01E: ".png",
    0xF01F: ".bmp",
    0xF029: ".tif",
    0xF02A: ".jpg",
}
METAFILES = {".emf", ".wmf", ".pict"}
LINKED = "linked image"
# A picture as extension and bytes, a skipped key when it was kept out, or None when
# there's no picture
Picture = tuple[str, bytes] | str | None


class RecordError(ValueError):
    """Drawing records this parser can't follow"""


class Blips:
    """Picture records unpacked once each, all against the document's picture total

    Any number of store entries can point at the same record, and each would otherwise
    copy or inflate it again
    """

    def __init__(self, embeds: Embeds) -> None:
        self.embeds = embeds
        self.read: dict[tuple[int, int], Picture] = {}

    def at(self, data: bytes, offset: int) -> Picture:
        key = (id(data), offset)
        if key not in self.read:
            self.read[key] = _blip(data, offset, self.embeds)
        return self.read[key]


def header(data: bytes, at: int) -> tuple[int, int, int, int]:
    """Type, instance, body start and end of the record at `at`"""
    if at < 0 or at + 8 > len(data):
        raise RecordError(f"record offset {at} outside the stream")
    ver_inst, kind, size = struct.unpack_from("<HHI", data, at)
    return kind, ver_inst >> 4, at + 8, min(at + 8 + size, len(data))


def children(data: bytes, start: int, end: int) -> Iterator[tuple[int, int, int, int]]:
    while start + 8 <= end:
        kind, inst, body, stop = header(data, start)
        yield kind, inst, body, min(stop, end)
        start = stop


def atoms(data: bytes, start: int, end: int) -> Iterator[tuple[int, int, int, int]]:
    """Every atom in a range in file order, opening containers in place

    A stack instead of recursion, since a crafted file can nest thousands deep
    """
    stack = [(start, end)]
    while stack:
        pos, stop = stack.pop()
        if pos + 8 > stop:
            continue
        ver_inst = struct.unpack_from("<H", data, pos)[0]
        kind, inst, body, inner = header(data, pos)
        stack.append((inner, stop))
        if ver_inst & 0xF == 0xF:
            stack.append((body, min(inner, stop)))
        else:
            yield kind, inst, body, min(inner, stop)


def props(data: bytes, inst: int, a: int, b: int) -> dict[int, int]:
    """Fixed shape properties by id, without the flag bits, keeping only picture ids"""
    found = {}
    for k in range(min(inst, (b - a) // 6)):
        pid, value = struct.unpack_from("<HI", data, a + 6 * k)
        # The fBid bit marks a value that is a picture store index
        if pid & 0x3FFF not in PICTURE_PROPS or pid & 0x4000:
            found[pid & 0x3FFF] = value
    return found


def wanted(found: dict[int, int]) -> int | None:
    """The store index a shape shows, -1 for a linked picture, None for no picture"""
    pib = next((found[p] for p in PICTURE_PROPS if found.get(p)), None)
    return pib if pib is not None else (-1 if PIB_NAME in found else None)


def _blip(data: bytes, at: int, embeds: Embeds) -> Picture:
    """One picture record as an extension and the bytes an image reader opens"""
    kind, inst, body, end = header(data, at)
    ext = BLIPS.get(kind)
    if ext is None:
        return None
    # Odd instances carry a second 16 byte id
    body += 16 * (1 + (inst & 1))
    if ext in METAFILES:
        if end - body < 34:
            return None
        size, compression = struct.unpack_from("<I", data, body)[0], data[body + 32]
        # The declared size bounds the inflate, so it's checked before anything unpacks
        packed = end - body - 34
        cap = (min(size, MAX_PART_BYTES) or MAX_PART_BYTES) if compression == 0 else packed
        if why := embeds.refusal(cap, packed if compression == 0 else None):
            return why
        raw = data[body + 34 : end]
        if compression == 0:
            raw = zlib.decompressobj().decompress(raw, cap)
    else:
        # Raster pictures have a one byte tag before the file, and a bitmap gains a header
        if why := embeds.refusal(end - body - 1 + 14 * (ext == ".bmp")):
            return why
        raw = data[body + 1 : end]
        if ext == ".bmp":
            raw = bmp(raw)
    kept = embeds.hold(raw)
    return OVER_TOTAL if kept is None else (ext, kept)


def bmp(dib: bytes) -> bytes:
    """A device independent bitmap with the file header it lacks"""
    if len(dib) < 40:
        return dib
    size, bits, compression, used = (
        struct.unpack_from("<I", dib, 0)[0],
        struct.unpack_from("<H", dib, 14)[0],
        struct.unpack_from("<I", dib, 16)[0],
        struct.unpack_from("<I", dib, 32)[0],
    )
    palette = used or (1 << bits if bits <= 8 else 0)
    masks = 12 if compression == 3 and size == 40 else 0
    offset = 14 + size + masks + 4 * palette
    return b"BM" + struct.pack("<IHHI", 14 + len(dib), 0, 0, offset) + dib


def entry(data: bytes, a: int, b: int, delayed: bytes, blips: Blips) -> Picture:
    """One store entry's picture, kept in the entry or at its offset in `delayed`

    None for an empty or deleted entry, and an empty picture for one that can't be read
    """
    if b - a < 36:
        return None
    refs, delay = struct.unpack_from("<II", data, a + 24)
    name = data[a + 33]
    try:
        if refs == 0:
            # Deleted pictures stay in the store until the file is compacted
            return None
        if b - a > 36 + name:
            return blips.at(data, a + 36 + name)
        return blips.at(delayed, delay)
    except (RecordError, zlib.error, struct.error):
        return "unreadable image"


def place(embeds: Embeds, at: Src, found: Picture, index: int) -> None:
    """Hand a store picture to `embeds`, or count why it stays unread"""
    if found is None:
        embeds.skipped["missing image"] += 1
    elif isinstance(found, str):
        embeds.skipped[found] += 1
    elif not found[1]:
        embeds.skipped["unreadable image"] += 1
    elif found[0] == ".pict":
        embeds.skipped["pict image"] += 1
    else:
        embeds.picture(at, found[1], f"image{index}{found[0]}")
