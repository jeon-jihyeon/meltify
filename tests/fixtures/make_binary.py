"""Binary containers the tests build themselves, since the libraries meltify uses only read them"""

import io
import struct
from pathlib import Path

import pytest

from tests.fixtures.make_docs import calendar_xlsx

SECTOR, MINI, CUTOFF = 512, 64, 4096
END, FREE, FAT_SECTOR, NONE = 0xFFFFFFFE, 0xFFFFFFFF, 0xFFFFFFFD, 0xFFFFFFFF


def _chain(table: list[int], start: int, count: int) -> None:
    for i in range(start, start + count):
        table[i] = i + 1 if i < start + count - 1 else END


def compound_file(tree: dict) -> bytes:
    """A version 3 compound file of nested {name: bytes or dict}, the container .msg uses

    olefile only reads these, so tests build their own
    """
    entries: list[dict] = [{"name": "Root Entry", "type": 5, "kids": []}]

    def add(node: dict, parent: int) -> None:
        for name, value in node.items():
            entries.append({"name": name, "type": 1 if isinstance(value, dict) else 2})
            index = len(entries) - 1
            entries[parent]["kids"].append(index)
            if isinstance(value, dict):
                entries[index]["kids"] = []
                add(value, index)
            else:
                entries[index]["data"] = value

    add(tree, 0)
    mini = bytearray()
    minifat: list[int] = []
    big: list[tuple[int, bytes]] = []
    for i, e in enumerate(entries):
        data = e.get("data")
        if data is None:
            continue
        if len(data) < CUTOFF:
            e["start"] = len(mini) // MINI if data else END
            count = -(-len(data) // MINI)
            minifat += [FREE] * count
            _chain(minifat, e["start"], count)
            mini += data.ljust(count * MINI, b"\0")
        else:
            big.append((i, data))

    sectors: list[bytes] = []

    def put(data: bytes) -> tuple[int, int]:
        count = -(-len(data) // SECTOR)
        start = len(sectors)
        sectors.extend(
            data[k * SECTOR : (k + 1) * SECTOR].ljust(SECTOR, b"\0") for k in range(count)
        )
        return start, count

    runs = []
    for i, data in big:
        entries[i]["start"], count = put(data)
        runs.append((entries[i]["start"], count))
    mini_start, mini_count = put(bytes(mini)) if mini else (END, 0)
    minifat_bytes = b"".join(struct.pack("<I", v) for v in minifat)
    minifat_start, minifat_count = put(minifat_bytes) if minifat else (END, 0)
    runs += [(mini_start, mini_count), (minifat_start, minifat_count)]

    for e in entries:
        kids = sorted(
            e.get("kids", []), key=lambda k: (len(entries[k]["name"]), entries[k]["name"].upper())
        )
        # A right-leaning chain is a valid, if unbalanced, sibling tree
        e["child"] = kids[0] if kids else NONE
        for n, k in enumerate(kids):
            entries[k]["right"] = kids[n + 1] if n + 1 < len(kids) else NONE
    directory = b""
    for i, e in enumerate(entries):
        name = e["name"].encode("utf-16-le") + b"\0\0"
        start = mini_start if i == 0 else e.get("start", 0)
        size = len(mini) if i == 0 else len(e.get("data", b""))
        directory += struct.pack(
            "<64sHBBIII16sIQQIQ",
            name,
            len(name),
            e["type"],
            1,
            NONE,
            e.get("right", NONE),
            e["child"],
            b"\0" * 16,
            0,
            0,
            0,
            start,
            size,
        )
    empty = struct.pack("<64sHBBIII16sIQQIQ", b"", 0, 0, 0, NONE, NONE, NONE, b"", 0, 0, 0, 0, 0)
    directory += empty * (-len(entries) % 4)
    dir_start, dir_count = put(directory)
    runs.append((dir_start, dir_count))

    fat_count = 1
    while (len(sectors) + fat_count) > fat_count * (SECTOR // 4):
        fat_count += 1
    fat = [FREE] * (fat_count * SECTOR // 4)
    for start, count in runs:
        if count:
            _chain(fat, start, count)
    fat_start = len(sectors)
    for k in range(fat_count):
        fat[fat_start + k] = FAT_SECTOR
    fat_bytes = b"".join(struct.pack("<I", v) for v in fat)
    put(fat_bytes)

    difat = [fat_start + k for k in range(fat_count)] + [FREE] * (109 - fat_count)
    header = struct.pack(
        "<8s16sHHHHH6sIIIIIIIII",
        b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",
        b"\0" * 16,
        0x3E,
        3,
        0xFFFE,
        9,
        6,
        b"\0" * 6,
        0,
        fat_count,
        dir_start,
        0,
        CUTOFF,
        minifat_start,
        minifat_count,
        END,
        0,
    ) + b"".join(struct.pack("<I", v) for v in difat)
    return header + b"".join(sectors)


def pad(data: bytes, to: int) -> bytes:
    return data + b"\x00" * (-len(data) % to)


def wmr(fn: int, body: bytes) -> bytes:
    body = pad(body, 2)
    return struct.pack("<IH", 3 + len(body) // 2, fn) + body


def wmf(*records: bytes, placeable: bool = False) -> bytes:
    body = b"".join(records) + struct.pack("<IH", 3, 0)
    header = struct.pack("<HHHIHIH", 1, 9, 0x0300, (18 + len(body)) // 2, 4, 64, 0)
    if not placeable:
        return header + body
    return (
        b"\xd7\xcd\xc6\x9a"
        + struct.pack("<HhhhhHIH", 0, 0, 0, 1000, 500, 1440, 0, 0)
        + header
        + body
    )


def wmf_textout(x: int, y: int, raw: bytes) -> bytes:
    return wmr(0x0521, struct.pack("<H", len(raw)) + pad(raw, 2) + struct.pack("<hh", y, x))


def locked_xlsx(path: Path, secret: str) -> Path:
    """An Excel workbook encrypted with `secret`, the way Excel saves one"""
    msoffcrypto = pytest.importorskip("msoffcrypto")
    with path.open("wb") as out:
        msoffcrypto.OfficeFile(io.BytesIO(calendar_xlsx())).encrypt(secret, out)
    return path
