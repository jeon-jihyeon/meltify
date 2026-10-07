"""Text that EMF and WMF pictures draw, read from their records without rendering them

Text records carry the string and where it starts, so a picture whose words are all text
records needs no OCR. Pictures drawn as outlines or bitmaps still go on to a render
"""

from __future__ import annotations

import importlib.util
import struct
import unicodedata
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path

from meltify.converters import Converted
from meltify.converters.embeds import Embeds
from meltify.evidence import Src

PLACEABLE = b"\xd7\xcd\xc6\x9a"
# Windows charset ids from LOGFONT, mapped to the codepage of their 8-bit text
CODEPAGES = {
    0: "cp1252",  # ANSI
    1: "cp1252",  # DEFAULT
    77: "mac_roman",
    128: "cp932",  # SHIFTJIS
    129: "cp949",  # HANGUL
    130: "johab",
    134: "gbk",  # GB2312
    136: "cp950",  # CHINESEBIG5
    161: "cp1253",
    162: "cp1254",
    163: "cp1258",
    177: "cp1255",
    178: "cp1256",
    186: "cp1257",
    204: "cp1251",  # RUSSIAN
    222: "cp874",  # THAI
    238: "cp1250",  # EASTEUROPE
    255: "cp437",  # OEM
}
SYMBOL_CHARSET = 2
# Matches the LibreOffice page renders, so OCR sees the same scale from either
DPI = 200
# Height in logical units for text drawn before any font is selected, like the stock font
DEFAULT_HEIGHT = 16.0

ETO_OPAQUE = 0x0002
ETO_CLIPPED = 0x0004
ETO_GLYPH_INDEX = 0x0010
ETO_NO_RECT = 0x0100
ETO_SMALL_CHARS = 0x0200
ETO_PDY = 0x2000
TA_UPDATECP = 0x0001
TA_HORIZONTAL = 0x0006
TA_RIGHT = 0x0002
TA_CENTER = 0x0006

GLYPHS = "glyph-index text"
SYMBOLS = "symbol font text"


@dataclass(frozen=True)
class Line:
    """One line of drawn text, starting at x and y in the picture's logical units"""

    text: str
    x: float
    y: float


@dataclass
class Scan:
    """What a metafile draws: text lines, and what only pixels can show"""

    kind: str  # emf or wmf
    lines: list[Line] = field(default_factory=list)
    # Bitmaps drawn into the picture, whose words only OCR reads
    bitmaps: int = 0
    # Text records that hold no decodable string, by what a needs entry calls them
    undecoded: Counter[str] = field(default_factory=Counter)

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)


@dataclass(frozen=True)
class Font:
    height: float = DEFAULT_HEIGHT
    charset: int = 1


@dataclass(frozen=True)
class State:
    """What a text record reads from the device context, saved and restored as a whole"""

    font: Font = Font()
    align: int = 0
    cp: tuple[float, float] = (0.0, 0.0)


@dataclass
class _Run:
    """Text records that continue one line, in the order the picture draws them"""

    stream: str
    x: float
    y: float
    end: float
    height: float
    text: str


def _width(text: str, height: float) -> float:
    # Without an advance array, wide CJK glyphs are about one em and the rest about half
    return sum(height if unicodedata.east_asian_width(c) in "WF" else height * 0.55 for c in text)


def _clean(text: str) -> str:
    # Symbol fonts land in the private use area, and none of those code points are words
    return "".join(
        " " if c in "\t\r\n" else c
        for c in text
        if c in "\t\r\n" or unicodedata.category(c) not in ("Cc", "Co", "Cs", "Cf")
    )


class _Reader:
    """Collects text records into lines, joining glyph-at-a-time text back into words"""

    def __init__(self, kind: str) -> None:
        self.scan = Scan(kind)
        self.runs: list[_Run] = []
        self.state = State()
        self.saved: list[State] = []

    def draw(
        self, stream: str, x: float, y: float, text: str, advance: float | None = None
    ) -> None:
        height = self.state.font.height
        text = _clean(text)
        width = advance if advance is not None and advance > 0 else _width(text, height)
        align = self.state.align
        if align & TA_UPDATECP:
            # The reference point is ignored, and the text moves the current position on
            x, y = self.state.cp
            self.state = replace(self.state, cp=(x + width, y))
        if align & TA_HORIZONTAL == TA_CENTER:
            x -= width / 2
        elif align & TA_HORIZONTAL == TA_RIGHT:
            x -= width
        if not text:
            return
        last = self.runs[-1] if self.runs else None
        em = max(height, last.height if last else 0)
        # One line shares a baseline give or take half an em, and kerning may pull a glyph back
        # a little over the one before it
        same = (
            last is not None
            and last.stream == stream
            and abs(y - last.y) <= em * 0.5
            and x >= last.end - em * 0.3
        )
        if not same:
            self.runs.append(_Run(stream, x, y, x + width, height, text))
            return
        gap = x - last.end
        # A gap wider than a typical space, about a quarter em, splits two words
        space = gap > em * 0.25 and not last.text.endswith(" ") and not text.startswith(" ")
        last.text += (" " if space else "") + text
        last.end = max(last.end, x + width)

    def save(self) -> None:
        self.saved.append(self.state)

    def restore(self, n: int) -> None:
        # A negative count steps back from the latest save, a positive one names a level
        depth = len(self.saved) + n if n < 0 else n - 1
        if 0 <= depth < len(self.saved):
            self.state = self.saved[depth]
            del self.saved[depth:]

    def done(self) -> Scan:
        for run in self.runs:
            text = " ".join(run.text.split())
            if text:
                self.scan.lines.append(Line(text, run.x, run.y))
        return self.scan


def _decode(raw: bytes, charset: int, reader: _Reader) -> str | None:
    if charset == SYMBOL_CHARSET:
        reader.scan.undecoded[SYMBOLS] += 1
        return None
    return raw.decode(CODEPAGES.get(charset, "cp1252"), "replace")


def is_emf(data: bytes) -> bool:
    return len(data) >= 88 and data[:4] == b"\x01\x00\x00\x00" and data[40:44] == b" EMF"


def is_wmf(data: bytes) -> bool:
    if data[:4] == PLACEABLE:
        return True
    # A standard header: memory or disk type, a 9-word header, then version 1 or 3
    return (
        data[:2] in (b"\x01\x00", b"\x02\x00")
        and data[2:4] == b"\x09\x00"
        and data[4:6] in (b"\x00\x01", b"\x00\x03")
    )


def scan(data: bytes) -> Scan | None:
    """Text lines and bitmaps a metafile draws, None for bytes that aren't EMF or WMF"""
    try:
        if is_emf(data):
            return _emf(data)
        if is_wmf(data):
            return _wmf(data)
    except struct.error:
        # Records are bounds-checked, so this is a header too short to hold its fields
        return None
    return None


# EMF record types
EMR_EOF = 14
EMR_SETTEXTALIGN = 22
EMR_MOVETOEX = 27
EMR_SAVEDC = 33
EMR_RESTOREDC = 34
EMR_SELECTOBJECT = 37
EMR_DELETEOBJECT = 40
EMR_COMMENT = 70
EMR_EXTCREATEFONTINDIRECTW = 82
EMR_EXTTEXTOUTA = 83
EMR_EXTTEXTOUTW = 84
EMR_POLYTEXTOUTA = 96
EMR_POLYTEXTOUTW = 97
EMR_SMALLTEXTOUT = 108
# BITBLT and STRETCHBLT may only fill with a brush, so they count when they carry a bitmap
EMR_BLITS = {76, 77}
EMR_BITMAPS = {78, 79, 80, 81, 114, 116}
# Stock fonts are selected by these indexes with the high bit set
STOCK_FONTS = {0x80000000 | n for n in (10, 11, 12, 13, 14, 16, 17)}

# EMF+ records inside EMR_COMMENT
EMFPLUS_HEADER = 0x4001
EMFPLUS_OBJECT = 0x4008
EMFPLUS_DRAWIMAGE = 0x401A
EMFPLUS_DRAWIMAGEPOINTS = 0x401B
EMFPLUS_DRAWSTRING = 0x401C
EMFPLUS_DRAWDRIVERSTRING = 0x4036
EMFPLUS_FONT = 6
EMFPLUS_CMAP_LOOKUP = 0x0001


def _emf_text(reader: _Reader, buf: bytes, pos: int, at: int, wide: bool) -> None:
    """One EmrText object at `at`, whose offsets count from the record start at `pos`"""
    x, y, n, off_string, options = struct.unpack_from("<iiIII", buf, at)
    off_dx = struct.unpack_from("<I", buf, at + (20 if options & ETO_NO_RECT else 36))[0]
    if options & ETO_GLYPH_INDEX:
        reader.scan.undecoded[GLYPHS] += 1
        return
    raw = buf[pos + off_string : pos + off_string + n * (2 if wide else 1)]
    if wide:
        text: str | None = raw.decode("utf-16-le", "replace")
    else:
        text = _decode(raw, reader.state.font.charset, reader)
    if text is None:
        return
    advance = None
    step = 8 if options & ETO_PDY else 4
    if off_dx and pos + off_dx + n * step <= len(buf):
        dx = struct.unpack_from(f"<{n * step // 4}i", buf, pos + off_dx)
        advance = float(sum(dx[:: step // 4]))
    reader.draw("gdi", x, y, text, advance)


def _emf_plus(reader: _Reader, data: bytes, fonts: dict[int, float]) -> bool | None:
    """EMF+ records of one comment, returning whether the header marks a dual picture"""
    dual = None
    p = 0
    while p + 12 <= len(data):
        kind, flags, size, size_data = struct.unpack_from("<HHII", data, p)
        if size < 12 or p + size > len(data):
            break
        body = data[p + 12 : p + 12 + size_data]
        if kind == EMFPLUS_HEADER:
            dual = bool(flags & 0x0001)
        elif kind == EMFPLUS_OBJECT and (flags >> 8) & 0x7F == EMFPLUS_FONT and len(body) >= 8:
            fonts[flags & 0xFF] = struct.unpack_from("<f", body, 4)[0]
        elif kind in (EMFPLUS_DRAWIMAGE, EMFPLUS_DRAWIMAGEPOINTS):
            reader.scan.bitmaps += 1
        elif kind == EMFPLUS_DRAWSTRING and len(body) >= 28:
            n = struct.unpack_from("<I", body, 8)[0]
            x, y = struct.unpack_from("<ff", body, 12)
            text = body[28 : 28 + 2 * n].decode("utf-16-le", "replace")
            height = fonts.get(flags & 0xFF, DEFAULT_HEIGHT)
            for i, part in enumerate(text.splitlines()):
                _draw_plus(reader, x, y + i * height * 1.2, part, height)
        elif kind == EMFPLUS_DRAWDRIVERSTRING and len(body) >= 16:
            options, _, n = struct.unpack_from("<III", body, 4)
            if not options & EMFPLUS_CMAP_LOOKUP:
                reader.scan.undecoded[GLYPHS] += 1
            elif len(body) >= 16 + 10 * n and n:
                text = body[16 : 16 + 2 * n].decode("utf-16-le", "replace")
                x, y = struct.unpack_from("<ff", body, 16 + 2 * n)
                _draw_plus(reader, x, y, text, fonts.get(flags & 0xFF, DEFAULT_HEIGHT))
        p += size
    return dual


def _draw_plus(reader: _Reader, x: float, y: float, text: str, height: float) -> None:
    # EMF+ fonts are objects of their own, so the GDI selection doesn't size this text
    gdi = reader.state
    reader.state = State(Font(height or DEFAULT_HEIGHT, gdi.font.charset))
    reader.draw("plus", x, y, text)
    reader.state = gdi


def _emf(buf: bytes) -> Scan:
    reader = _Reader("emf")
    fonts: dict[int, Font] = {}
    plus_fonts: dict[int, float] = {}
    dual = False
    pos = 0
    while pos + 8 <= len(buf):
        kind, size = struct.unpack_from("<II", buf, pos)
        if size < 8 or size % 4 or pos + size > len(buf) or kind == EMR_EOF:
            break
        if kind == EMR_COMMENT and size >= 16 and buf[pos + 12 : pos + 16] == b"EMF+":
            got = _emf_plus(reader, buf[pos + 16 : pos + size], plus_fonts)
            dual = dual or bool(got)
        elif not _emf_draw(reader, buf, pos, size, kind):
            _emf_state(reader, buf, pos, size, kind, fonts)
        pos += size
    if dual and any(r.stream == "plus" for r in reader.runs):
        # A dual picture draws everything twice, once in EMF+ and once in GDI for old readers
        reader.runs = [r for r in reader.runs if r.stream == "plus"]
    return reader.done()


def _emf_draw(reader: _Reader, buf: bytes, pos: int, size: int, kind: int) -> bool:
    """Read one EMF text record, False when the record draws no text"""
    end = pos + size
    if kind in (EMR_EXTTEXTOUTA, EMR_EXTTEXTOUTW) and size >= 76:
        _emf_text(reader, buf, pos, pos + 36, kind == EMR_EXTTEXTOUTW)
    elif kind in (EMR_POLYTEXTOUTA, EMR_POLYTEXTOUTW) and size >= 40:
        count = struct.unpack_from("<I", buf, pos + 36)[0]
        for i in range(min(count, (size - 40) // 40)):
            _emf_text(reader, buf, pos, pos + 40 + 40 * i, kind == EMR_POLYTEXTOUTW)
    elif kind == EMR_SMALLTEXTOUT and size >= 36:
        x, y, n, options = struct.unpack_from("<iiII", buf, pos + 8)
        at = pos + 36 + (0 if options & ETO_NO_RECT else 16)
        if options & ETO_GLYPH_INDEX:
            reader.scan.undecoded[GLYPHS] += 1
        elif options & ETO_SMALL_CHARS:
            text = _decode(buf[at : min(at + n, end)], reader.state.font.charset, reader)
            if text is not None:
                reader.draw("gdi", x, y, text)
        else:
            text = buf[at : min(at + 2 * n, end)].decode("utf-16-le", "replace")
            reader.draw("gdi", x, y, text)
    else:
        return False
    return True


def _emf_state(
    reader: _Reader, buf: bytes, pos: int, size: int, kind: int, fonts: dict[int, Font]
) -> None:
    """Follow one EMF record that changes what later text records draw with, or a bitmap"""
    if kind == EMR_EXTCREATEFONTINDIRECTW and size >= 12 + 28:
        index, height = struct.unpack_from("<Ii", buf, pos + 8)
        fonts[index] = Font(float(abs(height)) or DEFAULT_HEIGHT, buf[pos + 12 + 23])
    elif kind == EMR_SELECTOBJECT and size >= 12:
        index = struct.unpack_from("<I", buf, pos + 8)[0]
        if index in fonts:
            reader.state = replace(reader.state, font=fonts[index])
        elif index in STOCK_FONTS:
            reader.state = replace(reader.state, font=Font())
    elif kind == EMR_DELETEOBJECT and size >= 12:
        fonts.pop(struct.unpack_from("<I", buf, pos + 8)[0], None)
    elif kind == EMR_SETTEXTALIGN and size >= 12:
        reader.state = replace(reader.state, align=struct.unpack_from("<I", buf, pos + 8)[0])
    elif kind == EMR_MOVETOEX and size >= 16:
        reader.state = replace(reader.state, cp=struct.unpack_from("<ii", buf, pos + 8))
    elif kind == EMR_SAVEDC:
        reader.save()
    elif kind == EMR_RESTOREDC and size >= 12:
        reader.restore(struct.unpack_from("<i", buf, pos + 8)[0])
    elif kind in EMR_BITMAPS or (
        kind in EMR_BLITS and size >= 92 and struct.unpack_from("<I", buf, pos + 88)[0]
    ):
        reader.scan.bitmaps += 1


# WMF record functions
META_EOF = 0x0000
META_SAVEDC = 0x001E
META_RESTOREDC = 0x0127
META_SELECTOBJECT = 0x012D
META_SETTEXTALIGN = 0x012E
META_DELETEOBJECT = 0x01F0
META_MOVETO = 0x0214
META_TEXTOUT = 0x0521
META_EXTTEXTOUT = 0x0A32
META_CREATEFONTINDIRECT = 0x02FB
# Every object a record creates takes the lowest free slot, so all of them are counted
META_CREATES = {0x00F7, 0x0142, 0x01F9, 0x02FA, 0x02FB, 0x02FC, 0x06FF}
META_BITMAPS = {0x0922, 0x0940, 0x0B23, 0x0B41, 0x0D33, 0x0F43}


def _wmf(buf: bytes) -> Scan:
    reader = _Reader("wmf")
    pos = 22 if buf[:4] == PLACEABLE else 0
    pos += struct.unpack_from("<H", buf, pos + 2)[0] * 2
    # Objects by slot, since a selection names the slot its object took
    table: list[Font | str | None] = []
    while pos + 6 <= len(buf):
        words, fn = struct.unpack_from("<IH", buf, pos)
        if words < 3 or pos + words * 2 > len(buf) or fn == META_EOF:
            break
        if not _wmf_draw(reader, buf, pos, words, fn):
            _wmf_state(reader, buf, pos, words, fn, table)
        pos += words * 2
    return reader.done()


def _wmf_draw(reader: _Reader, buf: bytes, pos: int, words: int, fn: int) -> bool:
    """Read one WMF text record, False when the record draws no text"""
    p, end = pos + 6, pos + words * 2
    if fn == META_TEXTOUT and words >= 4:
        n = struct.unpack_from("<H", buf, p)[0]
        at = p + 2 + n + (n & 1)
        if at + 4 <= end:
            y, x = struct.unpack_from("<hh", buf, at)
            text = _decode(buf[p + 2 : p + 2 + n], _charset(reader), reader)
            if text is not None:
                reader.draw("gdi", x, y, text)
    elif fn == META_EXTTEXTOUT and words >= 7:
        y, x, n, options = struct.unpack_from("<hhHH", buf, p)
        at = p + 8 + (8 if options & (ETO_OPAQUE | ETO_CLIPPED) else 0)
        dx = at + n + (n & 1)
        if options & ETO_GLYPH_INDEX:
            reader.scan.undecoded[GLYPHS] += 1
        elif at + n <= end and (text := _decode(buf[at : at + n], _charset(reader), reader)):
            advance = None
            if dx + 2 * n <= end:
                advance = float(sum(struct.unpack_from(f"<{n}h", buf, dx)))
            reader.draw("gdi", x, y, text, advance)
    else:
        return False
    return True


def _wmf_state(
    reader: _Reader, buf: bytes, pos: int, words: int, fn: int, table: list[Font | str | None]
) -> None:
    """Follow one WMF record that changes what later text records draw with, or a bitmap"""
    p, size = pos + 6, words * 2
    if fn in META_CREATES:
        made: Font | str = "object"
        if fn == META_CREATEFONTINDIRECT and size >= 6 + 18:
            height = struct.unpack_from("<h", buf, p)[0]
            made = Font(float(abs(height)) or DEFAULT_HEIGHT, buf[p + 13])
        free = next((i for i, o in enumerate(table) if o is None), len(table))
        table[free : free + 1] = [made]
    elif fn == META_SELECTOBJECT and size >= 8:
        index = struct.unpack_from("<H", buf, p)[0]
        if index < len(table) and isinstance(font := table[index], Font):
            reader.state = replace(reader.state, font=font)
    elif fn == META_DELETEOBJECT and size >= 8:
        index = struct.unpack_from("<H", buf, p)[0]
        if index < len(table):
            table[index] = None
    elif fn == META_SETTEXTALIGN and size >= 8:
        reader.state = replace(reader.state, align=struct.unpack_from("<H", buf, p)[0])
    elif fn == META_MOVETO and size >= 10:
        y, x = struct.unpack_from("<hh", buf, p)
        reader.state = replace(reader.state, cp=(x, y))
    elif fn == META_SAVEDC:
        reader.save()
    elif fn == META_RESTOREDC and size >= 8:
        reader.restore(struct.unpack_from("<h", buf, p)[0])
    elif fn in META_BITMAPS and words > (fn >> 8) + 3:
        # The record carries a bitmap when it's longer than its fixed fields
        reader.scan.bitmaps += 1


def _charset(reader: _Reader) -> int:
    return reader.state.font.charset


def replay_available() -> bool:
    return importlib.util.find_spec("metafile_render") is not None


def replay(data: bytes) -> bytes | None:
    """PNG of a metafile drawn by metafile-render in pure Python, None when it can't draw it"""
    from metafile_render import MetafileResourceLimitError, render_metafile

    # A big canvas can pass its pixel budget at full resolution and still fit at screen dpi
    for dpi in (DPI, 96):
        try:
            return render_metafile(data, output_format="png", dpi=dpi).data
        except MetafileResourceLimitError:
            continue
        except Exception:  # noqa: BLE001
            # metafile-render raises its own errors on malformed records, and odd ones can
            # trip anything inside
            return None
    return None


def sniff(path: Path, head: bytes) -> bool:
    """An EMF or WMF picture under any name"""
    return is_emf(head) or is_wmf(head)


def convert(path: Path, src: Src) -> Converted:
    out = Converted("metafile")
    embeds = Embeds()
    data = path.read_bytes()
    suffix = path.suffix.lower()
    if suffix not in (".emf", ".wmf", ".emz", ".wmz"):
        # A sniffed picture keeps its kind, so a renderer gets the right extension
        suffix = ".emf" if is_emf(data) else ".wmf"
    embeds.picture(src, data, f"picture{suffix}")
    embeds.into(out)
    return out
