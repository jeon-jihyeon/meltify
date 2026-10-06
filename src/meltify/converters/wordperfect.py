"""WordPerfect documents, through libwpd's wpd2text or else LibreOffice

Without either a walker reads the text of WordPerfect 5 and 6 and later files, the ones
with a WPC header, and skips the function codes it knows the length of
"""

from __future__ import annotations

import struct
import subprocess
import tempfile
from pathlib import Path

from meltify import safe
from meltify.converters import Converted
from meltify.converters.render import convert_to, soffice
from meltify.converters.text import decode, numbered
from meltify.evidence import Src
from meltify.needs import count
from meltify.passwords import Locked, cant_decrypt
from meltify.tools import find

MAGIC = b"\xffWPC"
TIMEOUT = 120
HINT = "install libwpd for wpd2text, or LibreOffice"
WALKED = (
    "WordPerfect read by the built-in walker, so formatting codes may leak and headers, "
    "footers and footnotes are left out ({hint})"
)

# Lengths of the fixed length function groups, from libwpd's WP5 and WP6 file structures
WP5_FIXED = (4, 9, 11, 3, 3, 5, 6, 7, 4, 5, 6, 6, 8, 10, 10, 12)  # 0xC0 to 0xCF
WP6_FIXED = (4, 5, 3, 3, 3, 3, 4, 4, 4, 5, 5, 6, 6, 8, 8)  # 0xF0 to 0xFE
# WP6 stores common accented letters as single bytes 0x01 to 0x20
WP6_LOW = "åÅæÆäÄáàâãÃçÇëéÉèêíñÑøØõÕöÖüÜúùß"
# The start of WordPerfect character sets 1 and 4 as code points, after libwpd's maps. The
# first holds accents and accented letters, the second bullets, quotes and dashes
CHARSETS = {
    1: (
        *(0x0300, 0x00B7, 0x0303, 0x0302, 0x0335, 0x0338, 0x0301, 0x0308),
        *(0x0304, 0x0313, 0x0315, 0x02BC, 0x0326, 0x0315, 0x00B0, 0x0307),
        *(0x030B, 0x0327, 0x0328, 0x030C, 0x0337, 0x0305, 0x0306, 0x00DF),
        *(0x0138, 0x006A, 0x00C1, 0x00E1, 0x00C2, 0x00E2, 0x00C4, 0x00E4),
        *(0x00C0, 0x00E0, 0x00C5, 0x00E5, 0x00C6, 0x00E6, 0x00C7, 0x00E7),
        *(0x00C9, 0x00E9, 0x00CA, 0x00EA, 0x00CB, 0x00EB, 0x00C8, 0x00E8),
        *(0x00CD, 0x00ED, 0x00CE, 0x00EE, 0x00CF, 0x00EF, 0x00CC, 0x00EC),
        *(0x00D1, 0x00F1, 0x00D3, 0x00F3, 0x00D4, 0x00F4, 0x00D6, 0x00F6),
        *(0x00D2, 0x00F2, 0x00DA, 0x00FA, 0x00DB, 0x00FB, 0x00DC, 0x00FC),
        *(0x00D9, 0x00F9, 0x0178, 0x00FF, 0x00C3, 0x00E3, 0x0110, 0x0111),
        *(0x00D8, 0x00F8, 0x00D5, 0x00F5, 0x00DD, 0x00FD, 0x00D0, 0x00F0),
        *(0x00DE, 0x00FE),
    ),
    4: (
        *(0x25CF, 0x25CB, 0x25A0, 0x2022, 0x002A, 0x00B6, 0x00A7, 0x00A1),
        *(0x00BF, 0x00AB, 0x00BB, 0x00A3, 0x00A5, 0x20A7, 0x0192, 0x00AA),
        *(0x00BA, 0x00BD, 0x00BC, 0x00A2, 0x00B2, 0x207F, 0x00AE, 0x00A9),
        *(0x00A4, 0x00BE, 0x00B3, 0x201B, 0x2019, 0x2018, 0x201F, 0x201D),
        *(0x201C, 0x2013, 0x2014, 0x2039, 0x203A, 0x25CB, 0x25A1, 0x2020),
        *(0x2021, 0x2122, 0x2120, 0x211E, 0x25CF, 0x25E6, 0x25A0, 0x25AA),
        *(0x25A1, 0x25AB, 0x2012, 0xFB00, 0xFB03, 0xFB04, 0xFB01, 0xFB02),
        *(0x2026, 0x0024, 0x20A3, 0x20A2, 0x20A0, 0x20A4, 0x201A, 0x201E),
    ),
}
# Single byte functions that stand for whitespace, the rest are dropped
WP5_SINGLE = {0x8C: "\n", 0x90: "\n", 0x99: "\n", 0x93: " ", 0x94: " ", 0x95: " ", 0xA0: " "}
WP5_SINGLE |= dict.fromkeys((0xA9, 0xAA, 0xAB), "-")
WP6_SINGLE = {0x80: " ", 0x81: " ", 0x84: "-", 0x87: "\n", 0xC6: "\t"}
WP6_SINGLE |= dict.fromkeys(range(0xB4, 0xBA), "\n")  # deletable hard returns
WP6_SINGLE |= dict.fromkeys(range(0xBA, 0xBD), " ")  # deletable soft returns
WP6_SINGLE |= dict.fromkeys(range(0xBD, 0xC6), "\n")  # table rows and ends
WP6_SINGLE |= dict.fromkeys(range(0xC7, 0xCD), "\n")  # hard returns and page breaks
WP6_SINGLE |= dict.fromkeys(range(0xCD, 0xD0), " ")  # soft returns
# Soft end of line subgroups of the WP6 end of line group, which wrap without a break
WP6_SOFT = {0x01, 0x02, 0x03, 0x14, 0x15, 0x16}


class WpError(ValueError):
    """A file the walker can't read"""


def sniff(path: Path, head: bytes) -> bool:
    return head.startswith(MAGIC)


def _extended(char: int, charset: int) -> str | None:
    if charset == 0 and 0x20 <= char < 0x7F:
        return chr(char)
    found = CHARSETS.get(charset, ())
    return chr(found[char]) if char < len(found) else None


class _Walk:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.out: list[str] = []
        self.unmapped = 0

    def char(self, at: int) -> None:
        found = _extended(self.data[at + 1], self.data[at + 2])
        if found is None:
            self.unmapped += 1
            found = "\ufffd"
        self.out.append(found)

    def fixed(self, at: int, size: int) -> bool:
        """Whether a fixed length group ends with its own code, as libwpd checks"""
        return at + size <= len(self.data) and self.data[at + size - 1] == self.data[at]

    def wp5(self, at: int) -> None:
        data, n = self.data, len(self.data)
        while at < n:
            c = data[at]
            if 0x20 <= c <= 0x7E:
                self.out.append(chr(c))
            elif c in (0x0A, 0x0C):
                self.out.append("\n")
            elif c in (0x0B, 0x0D):
                self.out.append(" ")
            elif 0x80 <= c <= 0xBF:
                self.out.append(WP5_SINGLE.get(c, ""))
            elif 0xC0 <= c <= 0xCF and self.fixed(at, size := WP5_FIXED[c - 0xC0]):
                if c == 0xC0:
                    self.char(at)
                elif c in (0xC1, 0xC2):  # tab, center and indent
                    self.out.append("\t")
                at += size
                continue
            elif 0xD0 <= c <= 0xFE and at + 4 <= n:
                # code, subgroup, size, data, size, subgroup, code
                sub, size = data[at + 1], struct.unpack_from("<H", data, at + 2)[0]
                end = at + size + 4
                if (
                    end <= n
                    and data[end - 1] == c
                    and data[end - 2] == sub
                    and struct.unpack_from("<H", data, end - 4)[0] == size
                ):
                    at = end
                    continue
            at += 1

    def wp6(self, at: int) -> None:
        data, n = self.data, len(self.data)
        while at < n:
            c = data[at]
            if 0x01 <= c <= 0x20:
                self.out.append(WP6_LOW[c - 1])
            elif 0x21 <= c <= 0x7F:
                self.out.append(chr(c))
            elif 0x80 <= c <= 0xCF:
                self.out.append(WP6_SINGLE.get(c, ""))
            elif 0xF0 <= c <= 0xFE and self.fixed(at, size := WP6_FIXED[c - 0xF0]):
                if c == 0xF0:
                    self.char(at)
                at += size
                continue
            elif 0xD0 <= c <= 0xEF and at + 4 <= n:
                # code, subgroup, size counting the whole group, data, size, code
                sub, size = data[at + 1], struct.unpack_from("<H", data, at + 2)[0]
                end = at + size
                if (
                    size >= 7
                    and end <= n
                    and data[end - 1] == c
                    and struct.unpack_from("<H", data, end - 3)[0] == size
                ):
                    if c == 0xD0:
                        self.out.append(" " if sub in WP6_SOFT else "\n")
                    elif c == 0xE0:  # tab group
                        self.out.append("\t")
                    at = end
                    continue
            at += 1


def walk(data: bytes) -> tuple[str, int]:
    """Text of a WordPerfect 5 or 6 document, and how many characters had no mapping"""
    if not data.startswith(MAGIC) or len(data) < 16:
        raise WpError("file without a WPC header")
    start = struct.unpack_from("<I", data, 4)[0]
    file_type, major, minor = data[9], data[10], data[11]
    if file_type != 0x0A:
        raise WpError(f"file type {file_type}")
    if struct.unpack_from("<H", data, 12)[0]:
        # wpd2text only takes the password on its command line, where ps shows it
        raise Locked(cant_decrypt("WordPerfect passwords aren't supported"))
    if start > len(data):
        raise WpError("document offset past the end")
    w = _Walk(data)
    if major == 0:
        w.wp5(start)
    elif major == 2:
        w.wp6(start)
    else:
        raise WpError(f"version {major}.{minor}")
    lines = "".join(w.out).splitlines()
    return "\n".join(line.rstrip() for line in lines).strip("\n"), w.unmapped


def _wpd2text(tool: str, path: Path) -> str:
    # Bytes in, since its UTF-8 output shouldn't depend on the locale this runs under
    # An absolute path, so a file named like an option can't become one
    proc = safe.run([tool, str(path.resolve())], timeout=TIMEOUT, check=False, text=False)
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-1:]
        raise RuntimeError(f"wpd2text exited {proc.returncode}: {' '.join(tail)}")
    return proc.stdout.decode("utf-8", "replace")


def _hint(wpd2text: str | None, office: str | None) -> str:
    """What to install, or which installed reader failed, so the need names the real gap"""
    if wpd2text is None and office is None:
        return HINT
    if wpd2text is None:
        return "LibreOffice failed, install libwpd for wpd2text"
    if office is None:
        return "wpd2text failed, install LibreOffice"
    return "wpd2text and LibreOffice failed"


def convert(path: Path, src: Src) -> Converted:
    out = Converted("wordperfect")
    data = path.read_bytes()
    try:
        text, unmapped = walk(data)
    except WpError as e:
        text, unmapped, failed = None, 0, str(e)
    tool = find("wpd2text")
    if tool is not None:
        try:
            out.blocks = numbered(_wpd2text(tool, path), src)
            return out
        except (OSError, RuntimeError, subprocess.SubprocessError):
            pass
    # LibreOffice reads through libwpd too, so it beats the walker on headers and footnotes
    office = soffice()
    if office is not None:
        with tempfile.TemporaryDirectory(prefix="meltify-wpd-") as tmp:
            try:
                txt = convert_to(path, "txt:Text (encoded):UTF8", Path(tmp), TIMEOUT)
                out.blocks = numbered(decode(txt.read_bytes()), src)
                return out
            except (OSError, RuntimeError, subprocess.SubprocessError):
                pass
    hint = _hint(tool, office)
    if text is None:
        out.needs.append(f"WordPerfect {failed} not read ({hint})")
        return out
    out.blocks = numbered(text, src)
    out.needs.append(WALKED.format(hint=hint))
    if unmapped:
        out.needs.append(f"{count(unmapped, 'WordPerfect special character')} not mapped")
    return out
