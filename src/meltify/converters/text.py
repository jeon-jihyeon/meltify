from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from meltify.converters import Block, Converted
from meltify.evidence import Src

# Split large text, so each block stays small enough to quote with a line range
BLOCK_LINES = 200


def decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp949", "utf-16"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def numbered(text: str, src: Src) -> list[Block]:
    lines = text.splitlines()
    width = len(str(len(lines)))
    blocks = []
    for start in range(0, len(lines), BLOCK_LINES):
        chunk = lines[start : start + BLOCK_LINES]
        body = "\n".join(f"{start + i + 1:>{width}}| {line}" for i, line in enumerate(chunk))
        blocks.append(Block(replace(src, line=start + 1), body))
    return blocks


def convert(path: Path, src: Src) -> Converted:
    return Converted("text", numbered(decode(path.read_bytes()), src))


def convert_unknown(path: Path, src: Src) -> Converted:
    data = path.read_bytes()
    # A NUL byte near the start is the cheapest reliable sign of a binary format
    if b"\x00" in data[:4096]:
        return Converted("unknown", needs=["unsupported format"])
    return Converted("text", numbered(decode(data), src))
