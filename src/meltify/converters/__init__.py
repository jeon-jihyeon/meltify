"""Turn one input file into cited text blocks

Every converter returns blocks whose Src points back into the original file,
so you can quote the rendered markdown with a citation for each block.

A converter is a `convert(path, src) -> Converted` function in its own module here.
The registry below names it as "module:function" and imports it on first use, so a run
only loads the parsers its inputs need
"""

from __future__ import annotations

import bisect
import importlib
import itertools
import math
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from meltify.evidence import Src
from meltify.files import RAR_MAGIC, SEVEN_ZIP_MAGIC

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
    ".jp2",
    ".j2k",
    ".psd",
    ".ico",
    ".tga",
}
AUDIO = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".aiff", ".aif", ".amr"}
VIDEO = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".wmv", ".3gp", ".mpg", ".mpeg", ".flv"}
# Word and PowerPoint packages, macro and template variants included. office picks the
# family from the package's content types, so Hancom's .show rides along
OFFICE = {
    ".docx",
    ".docm",
    ".dotx",
    ".dotm",
    ".pptx",
    ".pptm",
    ".potx",
    ".potm",
    ".ppsx",
    ".ppsm",
    ".show",
}

# How many bytes a sniff sees, enough for magic numbers and a few chat lines
HEAD = 8192
# First line of every melted markdown file, so cleanups can tell meltify's files from others
SOURCE_MARK = "<!-- meltify source: "


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
    # False once subtitles gave a recording's transcript, so only its frames are read
    listen: bool = True

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
    # Whether the converter looks for hidden text at all, so read --hidden can say what it
    # couldn't check
    hidden_checked: bool = False
    # A result row per hidden span, when read --hidden asks for them
    spans: list[dict[str, Any]] = field(default_factory=list)
    # Page renders to keep beside the markdown, each with the place it shows. read moves
    # each file out and removes the folder that held it
    renders: list[tuple[Src, Path]] = field(default_factory=list)

    @property
    def chars(self) -> int:
        return sum(len(b.text) for b in self.blocks)

    def _markdown(self, origin: str) -> Iterator[str]:
        yield f"{SOURCE_MARK}{origin} -->"
        for b in self.blocks:
            yield f"\n## {b.src.cite()}\n{b.text.rstrip()}\n"
        if self.needs:
            yield f"\n<!-- meltify needs: {', '.join(self.needs)} -->"
        yield "\n"

    def markdown(self, origin: str) -> str:
        return "".join(self._markdown(origin))

    def write(self, target: Path, origin: str) -> None:
        """The markdown written block by block, so a big item never exists twice in memory"""
        with target.open("w", encoding="utf-8") as f:
            f.writelines(self._markdown(origin))


def place(blocks: list[Block], extra: list[Block]) -> list[Block]:
    """Merge extra blocks in after the page, slide, sheet or paragraph each belongs to

    An extra block for paragraph n goes before the first text block whose line is past n,
    since a text block's line is its paragraph number. When the text names a place the
    extra block doesn't, like a picture in an xlsb that cites no sheet, the block goes last
    """
    sheets = list(dict.fromkeys(b.src.sheet for b in blocks if b.src.sheet))
    paged = any(b.src.page for b in blocks)
    slides = any(b.src.slide for b in blocks)

    def order(src: Src, extra: bool) -> tuple[float, ...]:
        unnamed = math.inf if extra else 0
        flow = src.para if extra else src.line or src.para
        if flow:
            # A place in the paragraph flow outranks a table's name, since text documents
            # cite tables with `sheet` too and they sit between paragraphs
            sheet = 0
        elif src.sheet:
            sheet = sheets.index(src.sheet) + 1 if src.sheet in sheets else len(sheets) + 1
        else:
            sheet = unnamed if sheets else 0
        para = math.inf if src.para is None else src.para
        return (
            src.page or (unnamed if paged else 0),
            src.slide or (unnamed if slides else 0),
            sheet,
            para if extra else src.line or src.para or 0,
        )

    # Each extra goes before the first block, extras already placed included, whose key is
    # past its own. That block is also the first where the running max of keys passes it,
    # and running maxima are sorted, so a bisect finds it
    out = list(blocks)
    peaks = list(itertools.accumulate((order(b.src, False) for b in out), max))
    for b in extra:
        i = bisect.bisect_right(peaks, order(b.src, True))
        key = order(b.src, False)
        out.insert(i, b)
        peaks.insert(i, max(peaks[i - 1], key) if i else key)
        # Peaks after it rise to the new key until one is already past it
        for k in range(i + 1, len(peaks)):
            if peaks[k] >= key:
                break
            peaks[k] = key
    return out


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
# Checks for files a converter decrypts for itself, as "module:function" taking the path and
# its first bytes, so read never renders what would only show the lock
SEALS: list[str] = []


def register(kind: str, target: str, *suffixes: str) -> None:
    entry = Entry(kind, target)
    for s in suffixes:
        SUFFIXES[s.lower()] = entry


def register_sniff(
    kind: str, target: str, sniff: str | Sniff, over: set[str] | None = None
) -> None:
    """Pick a converter by content for unregistered suffixes or those in `over`

    Sniffs run in registration order
    """
    SNIFFS.append(Entry(kind, target, sniff, None if over is None else frozenset(over)))


def register_seal(target: str) -> None:
    SEALS.append(target)


def sealed_natively(path: Path, head: bytes) -> bool:
    """Whether a converter's own check finds the file encrypted or DRM-locked"""
    return any(_load(t)(path, head) for t in SEALS)


def _load(target: str) -> Callable:
    module, _, name = target.partition(":")
    return getattr(importlib.import_module(module), name)


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
        if head is None:
            head = _head(path)
        if sniff(path, head):
            return entry
    return None


def pick(path: Path) -> tuple[str, Converter]:
    suffix = suffix_of(path)
    entry = _sniffed(path, suffix) or SUFFIXES.get(suffix, UNKNOWN)
    return entry.kind, _load(entry.target)


def audio(path: Path, src: Src) -> Converted:
    return recording("audio", path, src)


def video(path: Path, src: Src) -> Converted:
    return recording("video", path, src)


def recording(kind: Literal["audio", "video"], path: Path, src: Src) -> Converted:
    """A recording, its transcript taken from subtitle files beside it when it has some"""
    from meltify import video

    return video.recording(kind, src, path, video.sidecars(path))


def _magic(*prefixes: bytes) -> Sniff:
    def sniff(path: Path, head: bytes) -> bool:
        return head.startswith(prefixes)

    return sniff


def _zip_mimetype(*types: bytes) -> Sniff:
    """ODF, EPUB and HWPX zips store their type uncompressed as the first member"""

    def sniff(path: Path, head: bytes) -> bool:
        return (
            head.startswith(b"PK\x03\x04")
            and head[30:38] == b"mimetype"
            and (head[38:].startswith(types))
        )

    return sniff


def _wordperfect(path: Path, head: bytes) -> bool:
    """A WordPerfect document, not WPG graphics, Presentations or Quattro sharing its header

    Byte 9 is the file type, and libwpd reads only 0x0A documents and 0x2C Mac WordPerfect
    """
    return head.startswith(b"\xffWPC") and len(head) > 9 and head[9] in (0x0A, 0x2C)


def _ooxml(*families: str) -> Sniff:
    """An OOXML package of these families under any name, so the zip sniff doesn't take it"""

    def sniff(path: Path, head: bytes) -> bool:
        if not head.startswith(b"PK\x03\x04"):
            return False
        from meltify.converters.ooxml import family

        return family(path) in families

    return sniff


_HERE = "meltify.converters"
UNKNOWN = Entry("unknown", f"{_HERE}.text:convert_unknown")

register("pdf", f"{_HERE}.pdf:convert", ".pdf", ".xps", ".oxps", ".fb2", ".cbz", ".mobi")
register("sheet", f"{_HERE}.sheet:convert", ".xlsx", ".xlsm", ".xltx", ".xltm", ".cell")
register("mail", f"{_HERE}.mail:convert", ".eml")
register("mail", f"{_HERE}.msg:convert", ".msg")
register("text", f"{_HERE}.text:convert", *TEXT)
register("image", f"{_HERE}.raster:convert", *IMAGES)
register("media", f"{_HERE}:audio", *AUDIO)
register("media", f"{_HERE}:video", *VIDEO)
register("office", f"{_HERE}.office:convert", *OFFICE)
register("web", f"{_HERE}.web:convert", ".html", ".htm", ".xhtml")
register("webarchive", f"{_HERE}.webarchive:convert", ".webarchive")
register("epub", f"{_HERE}.epub:convert", ".epub")
register(
    "archive",
    f"{_HERE}.archive:convert",
    *(".zip", ".tar", ".tgz", ".tar.gz", ".tbz2", ".tar.bz2", ".txz", ".tar.xz", ".7z", ".rar"),
    # One compressed file. The double suffixes above still win for tarballs
    *(".gz", ".bz2", ".xz"),
)
register("hwp", f"{_HERE}.hwp:convert", ".hwp", ".hwpx")
# Binary Office formats, with templates and slideshows sharing their document's format
register("legacy", f"{_HERE}.doc:convert", ".doc", ".dot")
register("legacy", f"{_HERE}.sheet:convert_binary", ".xls", ".xlt", ".xlsb")
register("legacy", f"{_HERE}.ppt:convert", ".ppt", ".pps", ".pot")
register(
    "odf",
    f"{_HERE}.odf:convert",
    *(".odt", ".ods", ".odp", ".odg", ".ott", ".ots", ".otp", ".otg"),
)
register("rtf", f"{_HERE}.rtf:convert", ".rtf")
register("iwork", f"{_HERE}.iwork:convert", ".pages", ".numbers", ".key")
register("svg", f"{_HERE}.svg:convert", ".svg", ".svgz")
register("metafile", f"{_HERE}.metafile:convert", ".emf", ".wmf", ".emz", ".wmz")
register("wordperfect", f"{_HERE}.wordperfect:convert", ".wpd", ".wp", ".wp5", ".wp6")
register("notebook", f"{_HERE}.notebook:convert", ".ipynb")
register("mbox", f"{_HERE}.mbox:convert", ".mbox")
register("sqlite", f"{_HERE}.sqlite:convert", ".db", ".sqlite", ".sqlite3")
register("parquet", f"{_HERE}.parquet:convert", ".parquet")
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
register_sniff("parquet", f"{_HERE}.parquet:convert", _magic(b"PAR1"))
register_sniff("wordperfect", f"{_HERE}.wordperfect:convert", _wordperfect)
# Document zips before the archive sniff, so an unnamed one isn't unpacked as a plain zip
for kind, target, families in (
    ("office", "office:convert", ("docx", "pptx")),
    ("sheet", "sheet:convert", ("xlsx",)),
    ("legacy", "sheet:convert_binary", ("xlsb",)),
):
    register_sniff(kind, f"{_HERE}.{target}", _ooxml(*families))
# Only the ODF kinds odf reads, so a formula or database zip stays an archive
ODF = (b"text", b"spreadsheet", b"presentation", b"graphics")
register_sniff(
    "odf",
    f"{_HERE}.odf:convert",
    _zip_mimetype(*(b"application/vnd.oasis.opendocument." + t for t in ODF)),
)
register_sniff("epub", f"{_HERE}.epub:convert", _zip_mimetype(b"application/epub+zip"))
register_sniff("hwp", f"{_HERE}.hwp:convert", _zip_mimetype(b"application/hwp+zip"))
register_sniff(
    "archive",
    f"{_HERE}.archive:convert",
    _magic(b"PK\x03\x04", SEVEN_ZIP_MAGIC, RAR_MAGIC),
)
register_sniff(
    "image",
    f"{_HERE}.raster:convert",
    _magic(
        *(b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"II*\x00", b"MM\x00*"),
        *(b"8BPS", b"\x00\x00\x00\x0cjP  \r\n\x87\n", b"\xff\x4f\xff\x51"),
    ),
)
# Its WMF check is loose, so it runs after every exact magic number above
register_sniff("metafile", f"{_HERE}.metafile:convert", f"{_HERE}.metafile:sniff")

# HWP and iWork decrypt inside their converters, so unlock can't tell their locks apart
register_seal(f"{_HERE}.hwp:sealed")
register_seal(f"{_HERE}.iwork:sealed")
