from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

from meltify.converters import Block, Converted
from meltify.evidence import Src

# vCard and iCalendar properties that hold whole files as base64
BINARY_PROPS = re.compile(r"^(?:[\w-]+\.)?(PHOTO|LOGO|SOUND|KEY|ATTACH)[;:]", re.I)
BASE64_PARAM = re.compile(r"ENCODING=(?:b|BASE64)\b", re.I)

# Split large text, so each block stays small enough to quote with a line range
BLOCK_LINES = 200


def decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp949", "utf-16"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def numbered(text: str, src: Src, first: int = 1) -> list[Block]:
    return numbered_lines(list(enumerate(text.splitlines(), start=first)), src)


def numbered_lines(lines: list[tuple[int, str]], src: Src) -> list[Block]:
    """Blocks of `N| text` rows where N is each row's line in the original file"""
    if not lines:
        return []
    width = len(str(max(n for n, _ in lines)))
    blocks = []
    for start in range(0, len(lines), BLOCK_LINES):
        chunk = lines[start : start + BLOCK_LINES]
        body = "\n".join(f"{n:>{width}}| {line}" for n, line in chunk)
        blocks.append(Block(replace(src, line=chunk[0][0]), body))
    return blocks


def convert(path: Path, src: Src) -> Converted:
    return Converted("text", numbered(decode(path.read_bytes()), src))


def convert_unknown(path: Path, src: Src) -> Converted:
    data = path.read_bytes()
    # A NUL byte near the start is the cheapest reliable sign of a binary format
    if b"\x00" in data[:4096]:
        return Converted("unknown", needs=["unsupported format"])
    return Converted("text", numbered(decode(data), src))


def unfold(text: str) -> list[tuple[int, str]]:
    """Logical lines of a vCard or iCalendar file, each kept at its first physical line

    A line that starts with a space or tab continues the previous one (RFC 6350 and 5545)
    """
    out: list[tuple[int, str]] = []
    for n, line in enumerate(text.splitlines(), start=1):
        if line[:1] in (" ", "\t") and out:
            out[-1] = (out[-1][0], out[-1][1] + line[1:])
        else:
            out.append((n, line))
    return out


def convert_card(path: Path, src: Src) -> Converted:
    lines = []
    for n, line in unfold(decode(path.read_bytes())):
        params, _, value = line.partition(":")
        prop = BINARY_PROPS.match(line)
        if prop and (BASE64_PARAM.search(params) or value.startswith("data:")):
            # A contact photo is tens of KB of base64 that no reader can quote
            line = f"{params}: [embedded data removed, {len(value):,} chars]"
        lines.append((n, line))
    return Converted("text", numbered_lines(lines, src))
