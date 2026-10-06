from __future__ import annotations

import codecs
import re
import sys
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

from meltify.converters import Block, Converted
from meltify.evidence import Src

# vCard and iCalendar properties that hold whole files as base64
BINARY_PROPS = re.compile(r"^(?:[\w-]+\.)?(PHOTO|LOGO|SOUND|KEY|ATTACH)[;:]", re.I)
BASE64_PARAM = re.compile(r"ENCODING=(?:b|BASE64)\b", re.I)

# Split large text, so each block stays small enough to quote with a line range
BLOCK_LINES = 200
# C0 controls and DEL, leaving out tab, newlines, form feed, the escape of ANSI colors and
# the four separators 0x1C to 0x1F that EDI and MARC records use between fields. Compressed
# or encrypted bytes have about one in ten of them, real text next to none
CONTROL = frozenset({*range(0x00, 0x09), 0x0B, *range(0x0E, 0x1B), 0x7F})
CONTROL_SHARE = 0.05
# How much of an unknown file decides whether it's text
SNIFF = 4096
# Bytes read at a time when a text file streams into blocks
CHUNK = 1 << 20
ENCODINGS = ("utf-8-sig", "cp949", "utf-16")


def decode(data: bytes) -> str:
    for encoding in ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def numbered(text: str, src: Src, first: int = 1) -> list[Block]:
    return numbered_lines(list(enumerate(text.splitlines(), start=first)), src)


def numbered_lines(lines: list[tuple[int, str]], src: Src, width: int | None = None) -> list[Block]:
    """Blocks of `N| text` rows where N is each row's line in the original file

    Numbers are padded to `width`, or to the widest of them when it's None
    """
    if not lines:
        return []
    width = width or len(str(max(n for n, _ in lines)))
    blocks = []
    for start in range(0, len(lines), BLOCK_LINES):
        chunk = lines[start : start + BLOCK_LINES]
        body = "\n".join(f"{n:>{width}}| {line}" for n, line in chunk)
        blocks.append(Block(replace(src, line=chunk[0][0]), body))
    return blocks


def _lines(path: Path, encoding: str, errors: str) -> Iterator[str]:
    """Lines of the file as `str.splitlines` cuts them, decoded a chunk at a time"""
    decoder = codecs.getincrementaldecoder(encoding)(errors)
    carry = ""
    with path.open("rb") as f:
        while True:
            chunk = f.read(CHUNK)
            final = not chunk
            text = carry + decoder.decode(chunk, final=final)
            bare, kept = text.splitlines(), text.splitlines(keepends=True)
            carry = ""
            # The last line may go on in the next chunk, and a CR there may be half of a CRLF
            if not final and kept and (kept[-1] == bare[-1] or kept[-1].endswith("\r")):
                carry = kept[-1]
                bare.pop()
            yield from bare
            if final:
                return


def _starts_with_bom(path: Path) -> bool:
    with path.open("rb") as f:
        return f.read(2) in (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)


def _encoding(path: Path) -> tuple[str, str, int]:
    """The encoding `decode` would pick, its error handling and how many lines it gives

    A pass over the whole file, since one bad byte near the end changes the encoding and
    the line count sets the width of every line number
    """
    for encoding in ENCODINGS:
        if encoding == "utf-16" and not _starts_with_bom(path):
            # bytes.decode reads UTF-16 without a byte order mark in native order, while
            # the incremental decoder refuses it
            encoding = f"utf-16-{'le' if sys.byteorder == 'little' else 'be'}"
        try:
            return encoding, "strict", sum(1 for _ in _lines(path, encoding, "strict"))
        except UnicodeDecodeError:
            continue
    return "utf-8", "replace", sum(1 for _ in _lines(path, "utf-8", "replace"))


def numbered_file(path: Path, src: Src) -> list[Block]:
    """The blocks `numbered(decode(path.read_bytes()), src)` gives, read a chunk at a time

    Neither the whole file nor its list of lines is ever held, so a big log or CSV costs
    about its own size once, in the blocks
    """
    encoding, errors, count = _encoding(path)
    width = len(str(count))
    blocks: list[Block] = []
    rows: list[str] = []
    first = 1
    for n, line in enumerate(_lines(path, encoding, errors), start=1):
        if not rows:
            first = n
        rows.append(f"{n:>{width}}| {line}")
        if len(rows) == BLOCK_LINES:
            blocks.append(Block(replace(src, line=first), "\n".join(rows)))
            rows = []
    if rows:
        blocks.append(Block(replace(src, line=first), "\n".join(rows)))
    return blocks


def convert(path: Path, src: Src) -> Converted:
    return Converted("text", numbered_file(path, src))


def binary(head: bytes) -> bool:
    """A NUL byte, or more control bytes than any text file has, near the start"""
    if b"\x00" in head:
        return True
    return sum(b in CONTROL for b in head) > len(head) * CONTROL_SHARE


def convert_unknown(path: Path, src: Src) -> Converted:
    with path.open("rb") as f:
        head = f.read(SNIFF)
    if binary(head):
        from meltify.converters import fallback

        return fallback.convert(path, src)
    return convert(path, src)


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
