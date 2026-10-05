"""Turn one input file into cited text blocks

Every converter returns blocks whose Src points back into the original file,
so you can quote the rendered markdown with a citation for each block.

A converter is a `convert(path, src) -> Converted` function in its own module here.
The registry below names it as "module:function" and imports it on first use, so a
module listed here can land later and `read` reports it as missing until then
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from meltify.evidence import Src

TEXT = {
    ".txt",
    ".md",
    ".csv",
    ".tsv",
    ".json",
    ".jsonl",
    ".log",
    ".xml",
    ".yaml",
    ".yml",
    ".srt",
    ".vtt",
}
IMAGES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
    ".heic",
    ".heif",
    ".avif",
}
AUDIO = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
VIDEO = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
MEDIA = AUDIO | VIDEO
OFFICE = {".docx", ".pptx", ".msg", ".html", ".htm", ".epub"}

# How many bytes a sniff sees, enough for magic numbers and a few chat lines
HEAD = 8192


@dataclass
class Block:
    src: Src
    text: str


@dataclass(frozen=True)
class RecognizeJob:
    """Pixels or sound that need OCR or ASR before their text can be cited

    `src` is where results cite back to. An OCR box is added to it as `bbox`, a
    transcript span as `t`
    """

    kind: Literal["image", "page", "audio", "video"]
    src: Src
    path: Path | None = None  # the file itself, or the PDF that holds a page
    data: bytes | None = None  # image bytes pulled out of a container
    # Where an embedded image is drawn on its PDF page in points, so pixel boxes map back
    rect: tuple[float, float, float, float] | None = None

    def __post_init__(self) -> None:
        if (self.path is None) == (self.data is None):
            raise ValueError("a recognize job needs exactly one of path or data")
        if self.kind == "page" and (self.path is None or self.src.page is None):
            raise ValueError("a page job needs the document path and src.page")


@dataclass(frozen=True)
class Child:
    """A file nested in a container, melted again through the registry

    `name` is the member path as the container stores it. `read` cleans it before it
    reaches a cite or the disk, so pass it raw. A `path` is moved into the output
    folder, so only hand over a file you own
    """

    name: str
    parent: Src
    data: bytes | None = None
    path: Path | None = None

    def __post_init__(self) -> None:
        if (self.path is None) == (self.data is None):
            raise ValueError("a child needs exactly one of path or data")


@dataclass
class Converted:
    kind: str
    blocks: list[Block] = field(default_factory=list)
    needs: list[str] = field(default_factory=list)
    children: list[Child] = field(default_factory=list)
    jobs: list[RecognizeJob] = field(default_factory=list)
    hidden: int = 0

    @property
    def chars(self) -> int:
        return sum(len(b.text) for b in self.blocks)

    def markdown(self, origin: str) -> str:
        parts = [f"<!-- meltify source: {origin} -->"]
        for b in self.blocks:
            parts.append(f"## {b.src.cite()}\n{b.text.rstrip()}\n")
        if self.needs:
            parts.append(f"<!-- meltify needs: {', '.join(self.needs)} -->")
        return "\n".join(parts) + "\n"


Converter = Callable[[Path, Src], Converted]
Sniff = Callable[[Path, bytes], bool]


@dataclass(frozen=True)
class Entry:
    kind: str
    target: str  # "module:function", imported on first use
    sniff: str | Sniff | None = None  # gets the path and its first HEAD bytes
    # Suffixes whose own entry a sniff may override. None limits it to unknown suffixes
    over: frozenset[str] | None = None


SUFFIXES: dict[str, Entry] = {}
SNIFFS: list[Entry] = []


def register(kind: str, target: str, *suffixes: str) -> None:
    entry = Entry(kind, target)
    for s in suffixes:
        SUFFIXES[s.lower()] = entry


def register_sniff(
    kind: str, target: str, sniff: str | Sniff, over: set[str] | None = None
) -> None:
    """Pick a converter by content, checked in registration order before any suffix"""
    SNIFFS.append(Entry(kind, target, sniff, None if over is None else frozenset(over)))


def _load(target: str) -> Callable | None:
    module, _, name = target.partition(":")
    try:
        return getattr(importlib.import_module(module), name)
    except ModuleNotFoundError as e:
        # Only the converter module itself may be absent, a broken dependency must still raise
        if e.name != module:
            raise
        return None
    except AttributeError:
        return None


def suffix_of(path: Path) -> str:
    """Longest registered suffix of the name, so `a.tar.gz` beats `.gz`"""
    double = "".join(path.suffixes[-2:]).lower()
    return double if double in SUFFIXES else path.suffix.lower()


def _head(path: Path) -> bytes:
    try:
        with path.open("rb") as f:
            return f.read(HEAD)
    except OSError:
        return b""


def _applies(entry: Entry, suffix: str) -> bool:
    return suffix not in SUFFIXES if entry.over is None else suffix in entry.over


def _sniffed(path: Path, suffix: str) -> Entry | None:
    head: bytes | None = None
    for entry in SNIFFS:
        if not _applies(entry, suffix):
            continue
        sniff = _load(entry.sniff) if isinstance(entry.sniff, str) else entry.sniff
        if sniff is None:
            continue
        if head is None:
            head = _head(path)
        if sniff(path, head):
            return entry
    return None


def _missing(kind: str, suffix: str) -> Converter:
    def convert(path: Path, src: Src) -> Converted:
        return Converted(kind, needs=[f"unsupported {suffix} (meltify 0.2 converter missing)"])

    return convert


def pick(path: Path) -> tuple[str, Converter]:
    suffix = suffix_of(path)
    entry = _sniffed(path, suffix) or SUFFIXES.get(suffix, UNKNOWN)
    convert = _load(entry.target)
    return entry.kind, convert or _missing(entry.kind, suffix or path.name)


def image(path: Path, src: Src) -> Converted:
    return Converted("image", jobs=[RecognizeJob("image", src, path=path)])


def audio(path: Path, src: Src) -> Converted:
    return Converted("media", jobs=[RecognizeJob("audio", src, path=path)])


def video(path: Path, src: Src) -> Converted:
    return Converted("media", jobs=[RecognizeJob("video", src, path=path)])


def _magic(*prefixes: bytes) -> Sniff:
    def sniff(path: Path, head: bytes) -> bool:
        return head.startswith(prefixes)

    return sniff


_HERE = "meltify.converters"
UNKNOWN = Entry("unknown", f"{_HERE}.text:convert_unknown")

register("pdf", f"{_HERE}.pdf:convert", ".pdf")
register("sheet", f"{_HERE}.sheet:convert", ".xlsx", ".xlsm")
register("mail", f"{_HERE}.mail:convert", ".eml")
register("text", f"{_HERE}.text:convert", *TEXT)
register("image", f"{_HERE}:image", *IMAGES)
register("media", f"{_HERE}:audio", *AUDIO)
register("media", f"{_HERE}:video", *VIDEO)
register("office", f"{_HERE}.office:convert", *OFFICE)
# 0.2 modules, each a `convert` in its own file
register(
    "archive",
    f"{_HERE}.archive:convert",
    *(".zip", ".tar", ".tgz", ".tar.gz", ".tbz2", ".tar.bz2", ".txz", ".tar.xz", ".7z", ".rar"),
)
register("hwp", f"{_HERE}.hwp:convert", ".hwp", ".hwpx")
register("legacy", f"{_HERE}.legacy:convert", ".doc", ".xls", ".ppt")
register("odf", f"{_HERE}.odf:convert", ".odt", ".ods", ".odp")
register("rtf", f"{_HERE}.rtf:convert", ".rtf")
register("iwork", f"{_HERE}.iwork:convert", ".pages", ".numbers", ".key")
register("svg", f"{_HERE}.svg:convert", ".svg")
register("notebook", f"{_HERE}.notebook:convert", ".ipynb")
register("mbox", f"{_HERE}.mbox:convert", ".mbox")
register("sqlite", f"{_HERE}.sqlite:convert", ".db", ".sqlite", ".sqlite3")
# Registered after TEXT, so these take over .srt and .vtt from the plain text entry
register("subtitle", f"{_HERE}.subtitle:convert", ".srt", ".vtt", ".ass")
register("text", f"{_HERE}.text:convert_card", ".vcf", ".ics")

# Chat exports look like plain text or zip archives, so only their content tells them apart
register_sniff("kakao", f"{_HERE}.kakao:convert", f"{_HERE}.kakao:sniff", {".txt", ".csv"})
register_sniff("slack", f"{_HERE}.slack:convert", f"{_HERE}.slack:sniff", {".zip"})
# Magic bytes for files whose name gives no hint
register_sniff("pdf", f"{_HERE}.pdf:convert", _magic(b"%PDF-"))
register_sniff("sqlite", f"{_HERE}.sqlite:convert", _magic(b"SQLite format 3\x00"))
register_sniff("rtf", f"{_HERE}.rtf:convert", _magic(b"{\\rtf"))
register_sniff("archive", f"{_HERE}.archive:convert", _magic(b"PK\x03\x04", b"7z\xbc\xaf\x27\x1c"))
register_sniff(
    "image",
    f"{_HERE}:image",
    _magic(b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a"),
)
