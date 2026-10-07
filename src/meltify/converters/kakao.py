"""KakaoTalk chat exports from the Windows, Android, iOS and macOS apps

Every message becomes `[YYYY-MM-DD HH:MM name] text` at its line in the original file,
so a cite like `chat.txt:123` still points at the export
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime
from pathlib import Path

from meltify.converters import Converted
from meltify.converters.text import decode, numbered_lines
from meltify.evidence import Src

MONTH_NAMES = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
MONTHS = {m: i for i, m in enumerate(MONTH_NAMES, start=1)}
TITLES = ("카카오톡 대화", "KakaoTalk Chats with")
CSV_HEADER = "Date,User,Message"

# Windows PC: a dashed date line, then `[name] [오후 8:36] text`
WIN_DAY = re.compile(r"^-+ (\d{4})년 (\d{1,2})월 (\d{1,2})일 \S+ -+$")
WIN_DAY_EN = re.compile(r"^-+ \w+, (\w+) (\d{1,2}), (\d{4}) -+$")
WIN_MSG = re.compile(r"^\[(.+?)\] \[(오전|오후) (\d{1,2}):(\d{2})\] ?(.*)$")
WIN_MSG_EN = re.compile(r"^\[(.+?)\] \[(\d{1,2}):(\d{2}) ?([AP]M)\] ?(.*)$")
# Android: `2021년 12월 29일 오후 8:36, name : text`
ANDROID = re.compile(r"^(\d{4})년 (\d{1,2})월 (\d{1,2})일 (오전|오후) (\d{1,2}):(\d{2}), (.*)$")
# iOS Korean: `2021. 12. 29. 오후 8:36, name : text`
IOS_KO = re.compile(r"^(\d{4})\. (\d{1,2})\. (\d{1,2})\.? (오전|오후) (\d{1,2}):(\d{2}), (.*)$")
# iOS English: `Dec 31, 2014 at 19:12, name : text`
IOS_EN = re.compile(
    r"^(\w{3})\w* (\d{1,2}), (\d{4}),? (?:at )?(\d{1,2}):(\d{2})(?: ?([AP]M))?, (.*)$"
)


def _hour(hour: str, half: str | None) -> int:
    h = int(hour)
    if half in ("오후", "PM"):
        return h % 12 + 12
    if half in ("오전", "AM"):
        return h % 12
    return h


def _stamp(y, mo, d, h, mi) -> str:
    return f"{int(y):04d}-{int(mo):02d}-{int(d):02d} {int(h):02d}:{int(mi):02d}"


def _who_said(rest: str) -> tuple[str, str]:
    # A message is `name : text`. Anything else on a dated line is a system notice
    name, sep, text = rest.partition(" : ")
    return (name, text) if sep else ("", rest)


def _message(line: str, day: tuple[int, int, int] | None) -> tuple[str, str, str] | None:
    """Time, sender and text when the line starts a message"""
    if m := ANDROID.match(line) or IOS_KO.match(line):
        y, mo, d, half, h, mi, rest = m.groups()
        return (_stamp(y, mo, d, _hour(h, half), mi), *_who_said(rest))
    if m := IOS_EN.match(line):
        mon, d, y, h, mi, half, rest = m.groups()
        if mon.lower() in MONTHS:
            return (_stamp(y, MONTHS[mon.lower()], d, _hour(h, half), mi), *_who_said(rest))
    if day and (m := WIN_MSG.match(line)):
        name, half, h, mi, text = m.groups()
        return (_stamp(*day, _hour(h, half), mi), name, text)
    if day and (m := WIN_MSG_EN.match(line)):
        name, h, mi, half, text = m.groups()
        return (_stamp(*day, _hour(h, half), mi), name, text)
    return None


def _day(line: str) -> tuple[int, int, int] | None:
    if m := WIN_DAY.match(line):
        return int(m[1]), int(m[2]), int(m[3])
    if (m := WIN_DAY_EN.match(line)) and m[1][:3].lower() in MONTHS:
        return int(m[3]), MONTHS[m[1][:3].lower()], int(m[2])
    return None


def _render(stamp: str, name: str, text: str) -> str:
    return f"[{stamp} {name}] {text}" if name else f"[{stamp}] {text}"


def parse_text(text: str) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    day = None
    for n, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip()
        if not line.strip():
            continue
        if found := _day(line):
            day = found
            continue
        if msg := _message(line, day):
            lines.append((n, _render(*msg)))
        else:
            # Multi-line messages, headers and date-only separators keep their own text
            lines.append((n, line))
    return lines


def parse_csv(text: str) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    reader = csv.reader(io.StringIO(text))
    next(reader, None)
    start = reader.line_num + 1
    for row in reader:
        if len(row) >= 3:
            try:
                when = datetime.fromisoformat(row[0].strip()).strftime("%Y-%m-%d %H:%M")
            except ValueError:
                when = row[0].strip()
            body = " ".join(row[2].splitlines())
            lines.append((start, _render(when, row[1].strip(), body)))
        start = reader.line_num + 1
    return lines


def _text(head: bytes) -> str:
    # The head may end mid-character, so drop the broken tail instead of failing
    for enc in ("utf-8-sig", "cp949"):
        try:
            return head.decode(enc)
        except UnicodeDecodeError as e:
            if e.start > len(head) - 4:
                return head[: e.start].decode(enc, "ignore")
    return head.decode("utf-8", "ignore")


def sniff(path: Path, head: bytes) -> bool:
    text = _text(head)
    first = text.lstrip("\ufeff").splitlines()[:3]
    if first and first[0].strip() == CSV_HEADER:
        return True
    if any(t in line for line in first for t in TITLES):
        return True
    day = None
    hits = 0
    # An export opens with a short header and then messages, so three within the first 60
    # lines mark one, while a note quoting a line or two of chat doesn't pass
    for line in text.splitlines()[:60]:
        day = _day(line) or day
        hits += _message(line, day) is not None
    return hits >= 3


def convert(path: Path, src: Src) -> Converted:
    text = decode(path.read_bytes())
    is_csv = text.lstrip("\ufeff").startswith(CSV_HEADER)
    lines = parse_csv(text) if is_csv else parse_text(text)
    return Converted("chat", numbered_lines(lines, src))
