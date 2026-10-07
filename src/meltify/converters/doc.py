"""Word 97 to 2003 binaries: text by line, and the pictures each paragraph shows

Templates (.dot) share the document's format
"""

from __future__ import annotations

import bisect
import re
import struct
import tempfile
import zlib
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

from meltify.converters import Converted
from meltify.converters.blips import (
    LINKED,
    PICTURE_ENTRY,
    SHAPE_OPTIONS,
    Blips,
    Picture,
    RecordError,
    atoms,
    children,
    entry,
    header,
    place,
    props,
    wanted,
)
from meltify.converters.embeds import Embeds
from meltify.converters.render import convert_to, soffice
from meltify.converters.text import decode, numbered
from meltify.evidence import Src
from meltify.needs import LIBREOFFICE, error_note
from meltify.passwords import LOCKED, Locked
from meltify.safe import MissingTool

OFFICE_HINT = "meltify doctor --install office"
SOFFICE_TIMEOUT = 180

# MS-DOC FibRgFcLcb97 entries, by their place among the fc and lcb pairs
FIB_CHPX, FIB_CLX, FIB_SPA, FIB_DGG = 12, 33, 40, 50
FIB_ENCRYPTED, FIB_TABLE = 0x0100, 0x0200
WORD97 = 0xC0  # lowest nFib Word 97 writers use. Word 95 and older lay the FIB out differently
PIC_LOCATION = 0x6A03  # sprmCPicLocation, the picture's offset in the Data stream
OLE_OBJECT = 0x080A  # sprmCFOle2, where the location names an object storage instead
FIELD_DATA = 0x0806  # sprmCFData, form field data rather than a picture
# Operand sizes by the sprm's top 3 bits, None for a length byte and that many bytes
SPRM_SIZES = (1, 1, 2, 4, 2, 2, None, 3)
FKP = 512
SHAPE_FILE = 0x66  # PICF mm when a picture name follows the header
DGG, BSTORE, DG, GROUP, SHAPE, FSP = 0xF000, 0xF001, 0xF002, 0xF003, 0xF004, 0xF00A
SPECIAL = re.compile("[\x01\x08]")
FLOATING = "\x08"


class DocError(RecordError):
    """A Word binary the picture reader can't follow"""


def convert(path: Path, src: Src) -> Converted:
    out = _text(path, src)
    try:
        _images(path, src).into(out)
    except ImportError:
        out.needs.append(f"doc pictures not read ({OFFICE_HINT})")
    except Locked as e:
        out.needs.append(f"doc pictures not read ({e})")
    except (ValueError, IndexError, OverflowError, OSError, struct.error, zlib.error) as e:
        # A file whose text wasn't read either is already a need as a whole
        if out.blocks or not out.needs:
            out.needs.append(f"doc pictures not read ({error_note(e)})")
    return out


def _text(path: Path, src: Src) -> Converted:
    out = Converted("legacy")
    failed = None
    try:
        from legacy_doc import extract_text
    except ImportError:
        extract_text = None
    if extract_text is not None:
        try:
            out.blocks = numbered(extract_text(path.read_bytes()).text, src)
            return out
        except Exception as e:  # noqa: BLE001
            # Word 95, odd encodings and oversized files land here, and soffice may still cope
            failed = error_note(e)
    if soffice() is None:
        if extract_text is None:
            raise MissingTool("legacy-doc", OFFICE_HINT)
        out.needs.append(f"doc ({failed}, {LIBREOFFICE} to retry)")
        return out
    with tempfile.TemporaryDirectory(prefix="meltify-doc-") as tmp:
        txt = convert_to(path, "txt:Text (encoded):UTF8", Path(tmp), SOFFICE_TIMEOUT)
        out.blocks = numbered(decode(txt.read_bytes()), src)
    return out


def _streams(path: Path) -> tuple[bytes, bytes, bytes]:
    """The WordDocument, table and Data streams of a Word 97 or later binary"""
    import olefile

    with olefile.OleFileIO(path) as ole:
        doc = ole.openstream("WordDocument").read()
        version, flags = struct.unpack_from("<H", doc, 2)[0], struct.unpack_from("<H", doc, 10)[0]
        if version < WORD97:
            raise DocError("Word 95 or older")
        if flags & FIB_ENCRYPTED:
            raise Locked(LOCKED)
        name = "1Table" if flags & FIB_TABLE else "0Table"
        if not ole.exists(name):
            raise DocError(f"no {name} stream")
        table = ole.openstream(name).read()
        data = ole.openstream("Data").read() if ole.exists("Data") else b""
    return doc, table, data


def _images(path: Path, src: Src) -> Embeds:
    """Inline and floating pictures of a Word 97 to 2003 binary, numbered in text order

    Inline ones sit in the Data stream where their character's properties point, floating
    ones in the drawing store that their anchor's shape names. Store pictures no anchor
    reaches, like header logos, follow without a paragraph. Drawn shapes such as rules and
    text boxes hold no picture and aren't counted
    """
    doc, table, data = _streams(path)
    embeds = Embeds()
    placed = _placed(doc, table, data, Blips(embeds), embeds)
    seen: set[bytes] = set()
    n = 0
    for para, found in placed:
        # A picture drawn twice holds the same text
        if isinstance(found, tuple) and found[1]:
            if found[1] in seen:
                continue
            seen.add(found[1])
        n += 1
        place(embeds, replace(src, para=para or None, img=n), found, n)
    return embeds


def _placed(
    doc: bytes, table: bytes, data: bytes, blips: Blips, embeds: Embeds
) -> list[tuple[int, Picture]]:
    """Each picture with its paragraph, as text order meets them, then the store's leftovers

    A leftover's paragraph is 0, since no anchor places it
    """
    ccp, pairs = _fib(doc)

    def pair(i: int) -> tuple[int, int]:
        return pairs[i] if i < len(pairs) else (0, 0)

    pieces = _pieces(table, *pair(FIB_CLX))
    text = "".join(
        doc[fc : fc + (end - cp) * width].decode("cp1252" if width == 1 else "utf-16-le", "replace")
        for cp, end, fc, width in pieces
    )[:ccp]
    runs = _runs(doc, table, *pair(FIB_CHPX))
    starts = [r[0] for r in runs]
    store = _bstore(doc, table, *pair(FIB_DGG), blips)
    shapes = _shapes(table, *pair(FIB_DGG))
    anchors = dict(_anchors(table, *pair(FIB_SPA)))
    placed: list[tuple[int, Picture]] = []
    used: set[int] = set()
    para, last = 1, 0
    for m in SPECIAL.finditer(text):
        cp = m.start()
        para, last = para + text.count("\r", last, cp), cp
        if m.group() == FLOATING:
            for pib in shapes.get(anchors.get(cp, -1), []):
                used.add(pib)
                placed.append((para, LINKED if pib < 0 else _nth(store, pib)))
            continue
        fc = _fc(pieces, cp)
        i = bisect.bisect_right(starts, fc) - 1
        sprms = _sprms(runs[i][2]) if i >= 0 and fc < runs[i][1] else {}
        if sprms.get(FIELD_DATA, b"\0")[0]:
            continue
        if sprms.get(OLE_OBJECT, b"\0")[0]:
            embeds.skipped["embedded object"] += 1
            continue
        at = sprms.get(PIC_LOCATION)
        found = _inline(data, struct.unpack("<I", at)[0], blips) if at and data else [None]
        placed += [(para, e) for e in found]
    placed += [(0, e) for n, e in enumerate(store, start=1) if e is not None and n not in used]
    return placed


def _nth(store: list[Picture], pib: int) -> Picture:
    return store[pib - 1] if 0 < pib <= len(store) else None


def _fib(doc: bytes) -> tuple[int, list[tuple[int, int]]]:
    """Main text length in characters, and the FIB's fc and lcb pairs"""
    at = 32
    at += 2 + 2 * struct.unpack_from("<H", doc, at)[0]
    longs = at + 2
    at = longs + 4 * struct.unpack_from("<H", doc, at)[0]
    ccp = struct.unpack_from("<i", doc, longs + 12)[0]
    count = struct.unpack_from("<H", doc, at)[0]
    pairs = [struct.unpack_from("<II", doc, at + 2 + 8 * i) for i in range(count)]
    return ccp, pairs


def _pieces(table: bytes, fc: int, lcb: int) -> list[tuple[int, int, int, int]]:
    """Text pieces as start and end character, file offset and bytes per character"""
    clx, at = table[fc : fc + lcb], 0
    # Property runs for fast saved files come before the piece table
    while at + 3 <= len(clx) and clx[at] == 1:
        at += 3 + struct.unpack_from("<H", clx, at + 1)[0]
    if at + 5 > len(clx) or clx[at] != 2:
        raise DocError("no piece table")
    plc = clx[at + 5 : at + 5 + struct.unpack_from("<I", clx, at + 1)[0]]
    n = (len(plc) - 4) // 12
    cps = struct.unpack_from(f"<{n + 1}I", plc)
    pieces = []
    for i in range(n):
        raw = struct.unpack_from("<I", plc, 4 * (n + 1) + 8 * i + 2)[0]
        if raw & 0x40000000:
            pieces.append((cps[i], cps[i + 1], (raw & 0x3FFFFFFF) // 2, 1))
        else:
            pieces.append((cps[i], cps[i + 1], raw, 2))
    return pieces


def _fc(pieces: list[tuple[int, int, int, int]], cp: int) -> int:
    for start, end, fc, width in pieces:
        if start <= cp < end:
            return fc + (cp - start) * width
    return -1


def _runs(doc: bytes, table: bytes, fc: int, lcb: int) -> list[tuple[int, int, bytes]]:
    """Character property runs in file order, as start and end offset and their sprms"""
    n = (lcb - 4) // 8
    runs = []
    for i in range(max(n, 0)):
        page = struct.unpack_from("<I", table, fc + 4 * (n + 1) + 4 * i)[0] & 0x3FFFFF
        fkp = doc[page * FKP : (page + 1) * FKP]
        if len(fkp) < FKP:
            raise DocError("character properties past the end")
        count = fkp[-1]
        fcs = struct.unpack_from(f"<{count + 1}I", fkp)
        for k in range(count):
            at = 2 * fkp[4 * (count + 1) + k]
            runs.append((fcs[k], fcs[k + 1], fkp[at + 1 : at + 1 + fkp[at]] if at else b""))
    return sorted(runs)


def _sprms(grpprl: bytes) -> dict[int, bytes]:
    found, at = {}, 0
    while at + 2 <= len(grpprl):
        sprm = struct.unpack_from("<H", grpprl, at)[0]
        at += 2
        size = SPRM_SIZES[sprm >> 13]
        if size is None:
            size = 1 + grpprl[at] if at < len(grpprl) else 0
        found[sprm] = grpprl[at : at + size]
        at += size
    return found


def _inline(data: bytes, at: int, blips: Blips) -> list[Picture]:
    """The pictures of one PICF record in the Data stream, none for a drawn shape"""
    size, head, mode = struct.unpack_from("<IHH", data, at)
    end, at = min(at + size, len(data)), at + head
    if mode == SHAPE_FILE:
        at += 1 + data[at]
    found: list[Picture] = []
    shape: dict[int, int] = {}
    for kind, inst, a, b in atoms(data, at, end):
        if kind == PICTURE_ENTRY:
            found.append(entry(data, a, b, b"", blips))
        elif kind == SHAPE_OPTIONS:
            shape |= props(data, inst, a, b)
    if found:
        return found
    pib = wanted(shape)
    return [] if pib is None else [LINKED if pib < 0 else None]


def _bstore(doc: bytes, table: bytes, fc: int, lcb: int, blips: Blips) -> list[Picture]:
    """The drawing store, whose entries keep their picture inline or in WordDocument"""
    if lcb < 8:
        return []
    kind, _, body, end = header(table, fc)
    if kind != DGG:
        raise DocError("drawing info without its container")
    for kind, _, a, b in children(table, body, end):
        if kind == BSTORE:
            return [
                entry(table, x, y, doc, blips)
                for k, _, x, y in children(table, a, b)
                if k == PICTURE_ENTRY
            ]
    return []


def _shapes(table: bytes, fc: int, lcb: int) -> dict[int, list[int]]:
    """Store indexes each top-level shape shows, by the shape id its anchor names

    A group's anchor names the group, so the pictures inside it count for the group
    """
    if lcb < 8:
        return {}
    *_, at = header(table, fc)
    shapes: dict[int, list[int]] = {}
    # Each drawing after the group follows a one byte label for the main text or headers
    while at + 9 <= fc + lcb:
        kind, _, body, end = header(table, at + 1)
        if kind != DG:
            break
        for kind, _, a, b in children(table, body, end):
            if kind != GROUP:
                continue
            for kind, _, x, y in children(table, a, b):
                if kind not in (GROUP, SHAPE):
                    continue
                spid, pibs = None, []
                for atom, inst, c, d in atoms(table, x, y):
                    if atom == FSP and spid is None and d - c >= 4:
                        spid = struct.unpack_from("<I", table, c)[0]
                    elif atom == SHAPE_OPTIONS and (pib := wanted(props(table, inst, c, d))):
                        pibs.append(pib)
                if spid is not None and pibs:
                    shapes[spid] = pibs
        at = end
    return shapes


def _anchors(table: bytes, fc: int, lcb: int) -> Iterator[tuple[int, int]]:
    """Main text position and shape id of each floating shape"""
    n = (lcb - 4) // 30
    for i in range(max(n, 0)):
        cp = struct.unpack_from("<I", table, fc + 4 * i)[0]
        yield cp, struct.unpack_from("<I", table, fc + 4 * (n + 1) + 26 * i)[0]
