from __future__ import annotations

import datetime as dt
import functools
import hashlib
import importlib.util
import io
import struct
import tempfile
import warnings
import xml.etree.ElementTree as ET
import zipfile
import zlib
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from meltify import passwords
from meltify.converters import Block, Child, Converted, RecognizeJob, iwa
from meltify.converters.limits import MAX_MEMBER_BYTES, MAX_RATIO, human_bytes, inflates
from meltify.converters.tables import escape_cell, grid, lettered
from meltify.converters.text import numbered_lines
from meltify.converters.xmlsafe import parse
from meltify.evidence import Src
from meltify.needs import count, not_read
from meltify.passwords import NEEDS_CRYPTO, Locked

# Newer iWork files keep a full-size preview at the root, older ones under QuickLook
PREVIEWS = ("preview.jpg", "QuickLook/Preview.jpg", "QuickLook/Thumbnail.jpg")
PREVIEW_PDF = "QuickLook/Preview.pdf"
MAX_PREVIEW_BYTES = 256 << 20
RASTER = {".png", ".jpg", ".jpeg", ".gif", ".tif", ".tiff", ".bmp", ".heic", ".webp"}
# Groups nest a few levels in real documents, so a deeper chain is built to blow the stack
MAX_DEPTH = 64
# How many skipped bundle links a needs line names before it just counts the rest
SHOW_LINKS = 5

# Apple's PBKDF2 count is 100k, so anything far past it is a file built to stall us
MAX_ITERATIONS = 10_000_000
# Files a locked bundle leaves readable, so the app can show the hint and metadata
PLAIN = (".iwph", ".iwpv2", "Metadata/")

# Message types, established against real documents by Docling and keynote-parser
KN_DOCUMENT, KN_SHOW, KN_SLIDE_NODE, KN_SLIDE, KN_PLACEHOLDER, KN_NOTE = 1, 2, 4, 5, 7, 15
TP_DOCUMENT = 10000
TSWP_STORAGE, TSWP_ATTACHMENT, TSWP_NOTE, TSWP_SHAPE = 2001, 2003, 2008, 2011
TSD_IMAGE, TSD_GROUP = 3005, 3008
TST_INFO, TST_MODEL, TST_TILE, TST_LIST, TST_TEXT_REF = 6000, 6001, 6002, 6005, 6218
TSCH_CHART = 5000
TSP_PACKAGE = 11006
# Image renditions from best to worst, since Pages doesn't always write all of them
IMAGE_FIELDS = (15, 13, 11, 12)
# Numbers counts dates in seconds from the start of 2001
EPOCH = dt.datetime(2001, 1, 1)


@dataclass
class Bundle:
    """The files of a package, whether a folder, a zip or a zip holding a flattened one"""

    names: list[str]
    size: Callable[[str], int]
    raw: Callable[[str], bytes]
    key: bytes | None = None
    # A zip member's compressed size, None for a file in a bundle folder
    packed: Callable[[str], int | None] = lambda name: None
    # Symbolic links in a bundle folder, which are never followed
    links: list[str] = field(default_factory=list)

    def read(self, name: str) -> bytes:
        if self.size(name) > MAX_MEMBER_BYTES:
            raise ValueError(f"{name} is over the {human_bytes(MAX_MEMBER_BYTES)} limit")
        data = self.raw(name)
        if self.key is None or name.startswith(PLAIN):
            return data
        return _decrypt(self.key, data) or data

    def has(self, name: str) -> bool:
        return name in self.names


@dataclass
class _Reader:
    """Walks one document's object graph into the converted output"""

    objs: dict[int, iwa.Obj]
    bundle: Bundle
    src: Src
    out: Converted
    data: dict[int, str] = field(default_factory=dict)
    seen: set[int] = field(default_factory=set)
    images: int = 0
    charts: int = 0
    # Drawables nested past MAX_DEPTH, left unread
    deep: int = 0

    def get(self, oid: int | None, kind: int) -> iwa.Obj | None:
        obj = self.objs.get(oid) if oid is not None else None
        return obj if obj is not None and obj.type == kind else None

    def paragraphs(self, storage: iwa.Obj) -> list[tuple[str, int, int]]:
        """Each paragraph's text with its start and end in UTF-16 units, as runs count them"""
        found = []
        start = 0
        for para in iwa.text(iwa.fields(storage.payload), 3).split("\n"):
            end = start + len(para.encode("utf-16-le")) // 2 + 1
            # U+FFFC marks where a drawable is anchored, which is read on its own
            clean = para.replace("\ufffc", "").replace("\u2028", " ").strip()
            found.append((clean, start, end))
            start = end
        return found

    def anchors(self, storage: iwa.Obj, n: int = 9) -> list[tuple[int, int]]:
        table = iwa.first(iwa.fields(storage.payload), n)
        if not isinstance(table, bytes):
            return []
        runs = []
        for entry in iwa.fields(table).get(1, []):
            if isinstance(entry, bytes):
                f = iwa.fields(entry)
                at, target = iwa.first(f, 1), iwa.ref(f, 2)
                if isinstance(at, int) and target is not None:
                    runs.append((at, target))
        return sorted(runs)

    def storage_lines(self, oid: int | None) -> list[str]:
        storage = self.get(oid, TSWP_STORAGE)
        if storage is None or oid in self.seen:
            return []
        self.seen.add(storage.id)
        return [t for t, _, _ in self.paragraphs(storage) if t]

    def drawable(self, oid: int, at: Src, depth: int = 0) -> list[str]:
        """Text of a drawable, with tables and pictures sent to their own blocks and jobs"""
        obj = self.objs.get(oid)
        if obj is None or oid in self.seen:
            return []
        if obj.type == TSWP_STORAGE:
            return self.storage_lines(oid)
        if depth > MAX_DEPTH:
            self.deep += 1
            return []
        # Marked before following any reference, so a cycle back to it ends here
        self.seen.add(oid)
        f = iwa.fields(obj.payload)
        if obj.type == TSWP_ATTACHMENT:
            target = iwa.ref(f, 1)
            return self.drawable(target, at, depth + 1) if target is not None else []
        if obj.type == KN_PLACEHOLDER:
            shape = iwa.first(f, 1)
            return (
                self.storage_lines(iwa.ref(iwa.fields(shape), 2))
                if isinstance(shape, bytes)
                else []
            )
        if obj.type == TSWP_SHAPE:
            return self.storage_lines(iwa.ref(f, 2))
        if obj.type == TSD_GROUP:
            return [t for child in iwa.refs(f, 2) for t in self.drawable(child, at, depth + 1)]
        if obj.type == TST_INFO:
            self.table(self.get(iwa.ref(f, 2), TST_MODEL), at)
        elif obj.type == TSD_IMAGE:
            self.picture(f, at)
        elif obj.type == TSCH_CHART:
            self.charts += 1
        return []

    def position(self, oid: int) -> tuple[float, float]:
        obj = self.objs.get(oid)
        return (_geometry(obj.payload) if obj else None) or (0.0, 0.0)

    def picture(self, f: iwa.Fields, at: Src) -> None:
        for n in IMAGE_FIELDS:
            name = self.data.get(iwa.ref(f, n) or -1)
            if name is None or not self.bundle.has(name):
                continue
            if self.bundle.size(name) > MAX_PREVIEW_BYTES:
                self.out.needs.append(f"iwork picture {name} over the size limit")
                return
            self.images += 1
            data = self.bundle.read(name)
            if Path(name).suffix.lower() in RASTER:
                self.out.jobs.append(RecognizeJob("image", replace(at, img=self.images), data=data))
            else:
                # A PDF or other vector picture melts through its own converter
                self.out.children.append(Child(name, self.src, data))
            return

    def table(self, model: iwa.Obj | None, at: Src) -> None:
        if model is None:
            return
        cells = _table_cells(model, self.objs)
        if cells:
            name = iwa.text(iwa.fields(model.payload), 8) or None
            # In Pages a table cites the paragraph it is anchored in
            where = replace(at, sheet=name, para=None, line=at.para)
            self.out.blocks.append(Block(where, grid(cells)))


def _load_data_names(objs: dict[int, iwa.Obj]) -> dict[int, str]:
    # TSP.PackageMetadata maps each data id to its file under Data/
    names: dict[int, str] = {}
    for obj in objs.values():
        if obj.type != TSP_PACKAGE:
            continue
        for entry in iwa.fields(obj.payload).get(4, []):
            if not isinstance(entry, bytes):
                continue
            f = iwa.fields(entry)
            oid, name = iwa.first(f, 1), iwa.text(f, 4) or iwa.text(f, 3)
            if isinstance(oid, int) and name:
                names[oid] = "Data/" + name
    return names


def _cell_values(store: iwa.Fields, objs: dict[int, iwa.Obj]) -> tuple[dict, dict]:
    """Shared strings and rich text, keyed the way packed cells reference them"""

    def read_list(n: int, decode: Callable[[iwa.Fields], str | None]) -> dict[int, str]:
        root = objs.get(iwa.ref(store, n) or -1)
        if root is None or root.type != TST_LIST:
            return {}
        payloads = [root.payload]
        payloads += [objs[s].payload for s in iwa.refs(iwa.fields(root.payload), 4) if s in objs]
        values = {}
        for payload in payloads:
            for entry in iwa.fields(payload).get(3, []):
                if isinstance(entry, bytes):
                    f = iwa.fields(entry)
                    key, value = iwa.first(f, 1), decode(f)
                    if isinstance(key, int) and value is not None:
                        values[key] = value
        return values

    def plain(f: iwa.Fields) -> str | None:
        value = iwa.first(f, 3)
        return value.decode("utf-8", "replace") if isinstance(value, bytes) else None

    def rich(f: iwa.Fields) -> str | None:
        link = objs.get(iwa.ref(f, 9) or -1)
        if link is None or link.type != TST_TEXT_REF:
            return None
        storage = objs.get(iwa.ref(iwa.fields(link.payload), 1) or -1)
        if storage is None or storage.type != TSWP_STORAGE:
            return None
        return iwa.text(iwa.fields(storage.payload), 3)

    return read_list(4, plain), read_list(17, rich)


def _decimal128(b: bytes) -> Decimal | None:
    if b[15] & 0x78 == 0x78:
        return None
    exponent = (((b[15] & 0x7F) << 7) | (b[14] >> 1)) - 6176
    coefficient = int.from_bytes(b[:14], "little") | ((b[14] & 1) << 112)
    value = Decimal(coefficient).scaleb(exponent)
    return -value if b[15] & 0x80 else value


def _number(value: Decimal | float, kind: int) -> str:
    if kind == 5:
        when = EPOCH + dt.timedelta(seconds=float(value))
        return when.date().isoformat() if when.time() == dt.time() else when.isoformat()
    if kind == 6:
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        value = Decimal(repr(value))
    return format(value.normalize(), "f")


def _cell(buf: bytes, at: int, strings: dict, rich: dict) -> str | None:
    """One packed TST cell, in the version 5 layout since 2017 or the older version 4

    Version 4 doesn't describe its own layout, so it is read the way real files
    write it and anything that doesn't fit yields nothing rather than misread bytes
    """
    if at < 0 or at + 12 > len(buf):
        return None
    version, kind = buf[at], buf[at + 1]
    if version == 5:
        flags = int.from_bytes(buf[at + 8 : at + 12], "little")
        pos = at + 12
        number: Decimal | float | None = None
        found: str | None = None
        for flag, width in ((1, 16), (2, 8), (4, 8), (8, 4), (16, 4)):
            if not flags & flag:
                continue
            if pos + width > len(buf):
                return None
            chunk = buf[pos : pos + width]
            if flag == 1:
                number = _decimal128(chunk)
            elif flag in (2, 4):
                number = struct.unpack("<d", chunk)[0]
            else:
                key = int.from_bytes(chunk, "little")
                found = (strings if flag == 8 else rich).get(key, found)
            pos += width
        if found is not None:
            return found
        return _number(number, kind) if number is not None else None
    if version == 4:
        flags = int.from_bytes(buf[at + 4 : at + 8], "little")
        ids = bin(flags).count("1")
        numeric = kind in (2, 5, 6, 7, 10)
        end = at + 12 + 4 * ids + (8 if numeric else 0)
        if not ids or end > len(buf):
            return None
        if numeric:
            return _number(struct.unpack("<d", buf[end - 12 : end - 4])[0], kind)
        if kind == 3:
            return strings.get(int.from_bytes(buf[end - 4 : end], "little"))
    return None


def _table_cells(model: iwa.Obj, objs: dict[int, iwa.Obj]) -> dict[tuple[int, int], str]:
    f = iwa.fields(model.payload)
    rows, cols, store_raw = iwa.first(f, 6), iwa.first(f, 7), iwa.first(f, 4)
    if not isinstance(rows, int) or not isinstance(cols, int) or not isinstance(store_raw, bytes):
        return {}
    store = iwa.fields(store_raw)
    strings, rich = _cell_values(store, objs)
    tiles = iwa.first(store, 3)
    cells: dict[tuple[int, int], str] = {}
    if not isinstance(tiles, bytes):
        return cells
    for entry in iwa.fields(tiles).get(1, []):
        if not isinstance(entry, bytes):
            continue
        e = iwa.fields(entry)
        tile = objs.get(iwa.ref(e, 2) or -1)
        first_row = iwa.first(e, 1)
        if tile is None or tile.type != TST_TILE:
            continue
        base = first_row if isinstance(first_row, int) else 0
        for row_raw in iwa.fields(tile.payload).get(5, []):
            if not isinstance(row_raw, bytes):
                continue
            row = iwa.fields(row_raw)
            r = iwa.first(row, 1)
            # Pages 5.2 added wider offsets scaled by 4 and kept the old pair for older apps
            buf, offsets = iwa.first(row, 6), iwa.first(row, 7)
            scale = 4 if iwa.first(row, 8) else 1
            if not isinstance(buf, bytes) or not isinstance(offsets, bytes):
                buf, offsets, scale = iwa.first(row, 3), iwa.first(row, 4), 1
            if (
                not isinstance(r, int)
                or not isinstance(buf, bytes)
                or not isinstance(offsets, bytes)
            ):
                continue
            for c in range(min(len(offsets) // 2, cols)):
                start = int.from_bytes(offsets[2 * c : 2 * c + 2], "little", signed=True)
                if start < 0 or base + r >= rows:
                    continue
                value = _cell(buf, start * scale, strings, rich)
                if value and (value := escape_cell(value.strip())):
                    cells[(base + r + 1, c + 1)] = value
    return cells


def _geometry(payload: bytes) -> tuple[float, float] | None:
    """Top and left of a drawable, from the TSD.GeometryArchive its chain of supers ends in

    Each super is field 1 of the one above, and the geometry's own field 1 is a point
    of two 32-bit floats, which is what tells it apart from another super
    """
    for _ in range(6):
        f = iwa.fields(payload)
        inner = iwa.first(f, 1)
        if not isinstance(inner, bytes):
            return None
        point = iwa.fields(inner)
        x, y = iwa.first(point, 1), iwa.first(point, 2)
        is_point = set(point) == {1, 2} and isinstance(x, bytes) and isinstance(y, bytes)
        if is_point and len(x) == len(y) == 4 and isinstance(iwa.first(f, 2), bytes):
            return struct.unpack("<f", y)[0], struct.unpack("<f", x)[0]
        payload = inner
    return None


def _keynote(r: _Reader) -> None:
    doc = next((o for o in r.objs.values() if o.type == KN_DOCUMENT), None)
    show = r.get(iwa.ref(iwa.fields(doc.payload), 2), KN_SHOW) if doc else None
    if show is None:
        return
    tree = iwa.first(iwa.fields(show.payload), 3)
    nodes = iwa.refs(iwa.fields(tree), 2) if isinstance(tree, bytes) else []
    slides: list[iwa.Obj] = []
    walked: set[int] = set()
    # A slide can sit under another in the navigator, and children follow their parent.
    # A stack instead of recursion, so a deep chain of nodes can't blow the call stack
    stack = nodes[::-1]
    while stack:
        oid = stack.pop()
        node = r.get(oid, KN_SLIDE_NODE)
        if node is None or oid in walked:
            continue
        walked.add(oid)
        f = iwa.fields(node.payload)
        if (slide := r.get(iwa.ref(f, 2), KN_SLIDE)) is not None:
            slides.append(slide)
        stack += iwa.refs(f, 1)[::-1]
    for n, slide in enumerate(slides, start=1):
        at = replace(r.src, slide=n)
        # Pictures are numbered within their slide, like pptx, ppt and odp
        r.images = 0
        tables = len(r.out.blocks)
        f = iwa.fields(slide.payload)
        number = iwa.ref(f, 20)
        lines: list[str] = []
        for placeholder in (iwa.ref(f, 5), iwa.ref(f, 6)):
            if placeholder is not None:
                lines += r.drawable(placeholder, at)
        # Shapes read top to bottom, then left to right, the order a viewer scans a slide
        rest = [d for d in dict.fromkeys(iwa.refs(f, 7)) if d != number]
        rest.sort(key=r.position)
        for d in rest:
            lines += r.drawable(d, at)
        note = r.get(iwa.ref(f, 27), KN_NOTE)
        spoken = r.storage_lines(iwa.ref(iwa.fields(note.payload), 1)) if note else []
        # The slide's text goes ahead of the tables found while walking it
        if body := _slide_text(lines, spoken):
            r.out.blocks.insert(tables, Block(at, body))


def _slide_text(shown: list[str], spoken: list[str]) -> str:
    body = "\n".join(shown)
    if spoken:
        body += "\n\nNotes:\n" + "\n".join(spoken)
    return body.strip()


def _numbered(r: _Reader, lines: list[tuple[int, str]], width: int) -> None:
    r.out.blocks += numbered_lines(lines, r.src, width)


def _pages(r: _Reader) -> None:
    doc = next((o for o in r.objs.values() if o.type == TP_DOCUMENT), None)
    if doc is None:
        return
    f = iwa.fields(doc.payload)
    body = r.get(iwa.ref(f, 4), TSWP_STORAGE)
    paras = r.paragraphs(body) if body else []
    width = len(str(len(paras)))
    # The cite line is the paragraph's position in the body, empty ones included.
    # A table or text box anchored in a paragraph shares its number
    lines: list[tuple[int, str]] = []
    anchored = r.anchors(body) if body else []
    if body is not None:
        r.seen.add(body.id)
    for n, (text, start, end) in enumerate(paras, start=1):
        if text:
            lines.append((n, text))
        for _, oid in (a for a in anchored if start <= a[0] < end):
            before = len(r.out.blocks)
            extra = r.drawable(oid, replace(r.src, para=n))
            if len(r.out.blocks) > before:
                # Keep the table after the text that leads up to it
                tables = r.out.blocks[before:]
                del r.out.blocks[before:]
                _numbered(r, lines, width)
                lines.clear()
                r.out.blocks += tables
            lines += [(n, t) for t in extra]
    # Text boxes outside the flow and footnotes continue the count after the body
    n = len(paras)
    floating = iwa.ref(f, 20)
    owned = iwa.all_refs(r.objs[floating].payload) if floating in r.objs else []
    for oid in dict.fromkeys(owned):
        for t in r.drawable(oid, replace(r.src, para=n + 1)):
            n += 1
            lines.append((n, t))
    for _, oid in r.anchors(body, 16) if body else []:
        note = r.get(oid, TSWP_NOTE)
        for t in r.storage_lines(iwa.ref(iwa.fields(note.payload), 2)) if note else []:
            n += 1
            lines.append((n, t))
    _numbered(r, lines, width)


def _flat_prefix(names: list[str]) -> str:
    # Keynote 2018 flattens the package into one folder and zips its index again
    if any(n.startswith(("Index/", "Index.zip", "index.")) for n in names):
        return ""
    for n in sorted(names):
        if n.endswith("/Index.zip") and n.count("/") == 1:
            return n[: -len("Index.zip")]
    return ""


def _folder(path: Path) -> Bundle:
    """A bundle folder's files, leaving out links, which could point anywhere on disk"""
    files: dict[str, Path] = {}
    links: list[str] = []
    for f in sorted(path.rglob("*")):
        name = f.relative_to(path).as_posix()
        if f.is_symlink():
            links.append(name)
        elif f.is_file():
            files[name] = f
    return Bundle(
        list(files),
        lambda n: files[n].stat().st_size,
        lambda n: files[n].read_bytes(),
        links=links,
    )


def _open(path: Path, z: zipfile.ZipFile | None) -> Bundle:
    if z is None:
        return _folder(path)
    infos = {i.filename: i for i in z.infolist() if not i.is_dir()}
    prefix = _flat_prefix(list(infos))
    names = [n[len(prefix) :] for n in infos if n.startswith(prefix)]
    return Bundle(
        names,
        lambda n: infos[prefix + n].file_size,
        lambda n: z.read(infos[prefix + n]),
        packed=lambda n: infos[prefix + n].compress_size,
    )


Member = tuple[int, int | None, Callable[[], bytes]]


def _index(bundle: Bundle, out: Converted) -> Iterator[bytes]:
    """The Index/*.iwa files one at a time, so each can go once it's decompressed"""
    if not bundle.has("Index.zip"):
        names = [n for n in bundle.names if n.startswith("Index/") and n.endswith(".iwa")]
        members = [
            (bundle.size(n), bundle.packed(n), functools.partial(bundle.read, n)) for n in names
        ]
        yield from _capped(members, None, out)
        return
    # Index files are Snappy data already, so one that deflates past the ratio is built to bloat
    if inflates(bundle.size("Index.zip"), bundle.packed("Index.zip")):
        out.needs.append(f"iwork Index.zip not read (expands over {MAX_RATIO}:1)")
        return
    with zipfile.ZipFile(io.BytesIO(bundle.read("Index.zip"))) as inner:
        infos = [i for i in inner.infolist() if i.filename.endswith(".iwa")]
        members = [(i.file_size, i.compress_size, functools.partial(inner.read, i)) for i in infos]
        yield from _capped(members, bundle.key, out)


def _capped(members: list[Member], key: bytes | None, out: Converted) -> Iterator[bytes]:
    """Index files within one total for the document, decrypted with `key` when given

    Snappy output is never much smaller than its input, so files past the cap on
    decompressed streams couldn't be decompressed anyway
    """
    left = iwa.MAX_STREAM_BYTES
    skipped: Counter[str] = Counter()
    for size, packed, read in members:
        if inflates(size, packed):
            skipped[f"expands over {MAX_RATIO}:1"] += 1
            continue
        if size > left:
            skipped[f"over the {human_bytes(iwa.MAX_STREAM_BYTES)} index total"] += 1
            continue
        left -= size
        data = read()
        yield data if key is None else _decrypt(key, data) or data
    for why, n in skipped.items():
        out.needs.append(not_read(n, "iwork index file", why))


def _decrypt(key: bytes, data: bytes) -> bytes | None:
    """One file of a locked bundle: an IV, AES-128-CBC with PKCS7, then 20 trailing bytes

    The first plaintext block is filler. None when the bytes don't decrypt, which is
    how a file the bundle left in the clear looks
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    body = data[16:-20]
    if len(data) < 52 or len(body) % 16 or iwa.is_iwa(data):
        return None
    plain = Cipher(algorithms.AES(key), modes.CBC(data[:16])).decryptor()
    out = plain.update(body) + plain.finalize()
    pad = out[-1]
    if not 1 <= pad <= 16 or out[-pad:] != bytes([pad]) * pad or len(out) - pad < 16:
        return None
    return out[16:-pad]


def _unlock(bundle: Bundle) -> None:
    """Derive the key from the .iwpv2 verifier, raising Locked when it can't be

    The verifier is version and format words, a PBKDF2-SHA1 count, a salt, an IV and
    64 bytes whose second half is the SHA-256 of the first half once decrypted
    """
    secret = passwords.password()
    if secret is None:
        raise Locked(passwords.LOCKED)
    if importlib.util.find_spec("cryptography") is None:
        raise Locked(NEEDS_CRYPTO)
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    verifier = bundle.read(".iwpv2")
    if len(verifier) != 104:
        raise ValueError("unrecognized .iwpv2 password verifier")
    version, kind, rounds = struct.unpack_from("<HHI", verifier)
    if (version, kind) != (2, 1) or not 0 < rounds <= MAX_ITERATIONS:
        raise ValueError(f"unsupported iWork encryption {version}.{kind}")
    salt, iv, check = verifier[8:24], verifier[24:40], verifier[40:]
    key = hashlib.pbkdf2_hmac("sha1", secret.encode("utf-8"), salt, rounds, 16)
    plain = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor().update(check)
    if hashlib.sha256(plain[:32]).digest() != plain[32:]:
        raise Locked(passwords.WRONG)
    bundle.key = key


def _cell_text(cell: Any) -> str:
    from numbers_parser import MergedCell

    # A merged range shows its value once, at the top-left cell
    if isinstance(cell, MergedCell):
        return ""
    return escape_cell(cell.formatted_value or "")


def _numbers(path: Path, src: Src, out: Converted) -> None:
    from numbers_parser import Document

    # numbers-parser warns about every document version it hasn't seen, which isn't actionable
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        doc = Document(path)
    for sheet in doc.sheets:
        for table in sheet.tables:
            rows = [
                (n, [_cell_text(c) for c in row])
                for n, row in enumerate(table.iter_rows(), start=1)
            ]
            text = lettered((n, r) for n, r in rows if any(r))
            if text is not None:
                table_src = replace(src, sheet=f"{sheet.name}>{table.name}")
                out.blocks.append(Block(table_src, text))


def _numbers_unlocked(bundle: Bundle, src: Src, out: Converted) -> None:
    # numbers-parser reads a plain package, so the decrypted files go to a private temp zip
    with tempfile.TemporaryDirectory() as tmp:
        plain = Path(tmp) / "unlocked.numbers"
        with zipfile.ZipFile(plain, "w", zipfile.ZIP_DEFLATED) as z:
            for name in bundle.names:
                if name not in (".iwph", ".iwpv2"):
                    z.writestr(name, bundle.read(name))
        _numbers(plain, src, out)


SF = "{http://developer.apple.com/namespaces/sf}"
SFA = "{http://developer.apple.com/namespaces/sfa}"
SL = "{http://developer.apple.com/namespaces/sl}"
KEY = "{http://developer.apple.com/namespaces/keynote2}"
PARA, TABLE = f"{SF}p", f"{SF}tabular-model"
STORAGE, ATTACHMENT, ATTACHMENT_REF = f"{SF}text-storage", f"{SF}attachment", f"{SF}attachment-ref"
# Page furniture and comments keep their own text, outside the body flow, and section
# prototypes are the template's sample pages
FURNITURE = {f"{SF}{t}" for t in ("header", "footer", "footnotes", "annotations")}
FURNITURE.add(f"{SL}section-prototypes")
GHOST = {f"{SF}ghost-text", f"{SF}ghost-text-ref"}


def _xml_text(el: ET.Element) -> str:
    # Template placeholder text shows before anyone types, so it isn't content
    parts: list[str] = []
    # Elements still to walk with each one's tail queued behind it, a stack instead of
    # recursion so deep XML can't blow the call stack
    stack: list[ET.Element | str] = [el]
    while stack:
        node = stack.pop()
        if isinstance(node, str):
            parts.append(node)
            continue
        if node.tag in GHOST:
            continue
        if node.tag in (f"{SF}br", f"{SF}lnbr"):
            parts.append(" ")
        elif node.tag == f"{SF}tab":
            parts.append("\t")
        parts.append(node.text or "")
        for child in reversed(node):
            stack += [child.tail or "", child]
    return "".join(parts).replace("\ufffc", "").strip()


def _paras(el: ET.Element) -> Iterator[ET.Element]:
    """Paragraphs and tables in reading order, leaving a table's cells inside it"""
    stack = list(reversed(el))
    while stack:
        child = stack.pop()
        if child.tag in FURNITURE or child.tag in GHOST:
            continue
        if child.tag in (PARA, TABLE):
            yield child
        else:
            stack += reversed(child)


def _xml_table(model: ET.Element) -> dict[tuple[int, int], str]:
    # Cells sit flat in the datasource in row-major order, the grid gives their places
    grid = next(model.iter(f"{SF}grid"), None)
    source = next(grid.iter(f"{SF}datasource"), None) if grid is not None else None
    cols = int(grid.get(f"{SF}numcols") or 0) if grid is not None else 0
    cells: dict[tuple[int, int], str] = {}
    for i, cell in enumerate(source if source is not None and cols else []):
        shown = next(cell.iter(f"{SF}ct"), None)
        if shown is not None:
            value = shown.get(f"{SFA}s") or "".join(shown.itertext())
        else:
            value = cell.get(f"{SF}v", "") if cell.tag == f"{SF}n" else ""
        if value := escape_cell(value.strip()):
            cells[(i // cols + 1, i % cols + 1)] = value
    return cells


def _legacy(bundle: Bundle, name: str, r: _Reader) -> None:
    """iWork '09 kept the document as plain XML, gzipped or not"""
    data = bundle.read(name)
    if name.endswith(".gz"):
        inflate = zlib.decompressobj(wbits=31)
        data = inflate.decompress(data, MAX_MEMBER_BYTES)
        if inflate.unconsumed_tail:
            raise ValueError(f"{name} expands past the {human_bytes(MAX_MEMBER_BYTES)} limit")
    root = parse(data)
    if name.startswith("index.apxl"):
        slides = [s for lst in root.iter(f"{KEY}slide-list") for s in lst if s.tag == f"{KEY}slide"]
        for n, slide in enumerate(slides, start=1):
            at = replace(r.src, slide=n)
            notes = [p for el in slide.iter(f"{KEY}notes") for p in el.iter(PARA)]
            skip = set(map(id, notes))
            found = [el for el in _paras(slide) if id(el) not in skip]
            shown = [t for p in found if p.tag == PARA and (t := _xml_text(p))]
            spoken = [t for p in notes if (t := _xml_text(p))]
            if body := _slide_text(shown, spoken):
                r.out.blocks.append(Block(at, body))
            tables = [cells for el in found if el.tag == TABLE and (cells := _xml_table(el))]
            r.out.blocks += [Block(at, grid(cells)) for cells in tables]
        return
    body = next((el for el in root if el.tag == STORAGE and el.get(f"{SF}kind") == "body"), None)
    flow = None if body is None else body.find(f"{SF}text-body")
    if flow is None:
        # A layout document has no body flow, so its text is read in document order
        _flow(r, list(_paras(root)), {}, [])
        return
    # Text boxes outside the flow continue the count after the body, one number for each
    # paragraph with text as in newer Pages
    floating = [
        el
        for group in root.iterfind(f"{SL}drawables")
        for kind in group
        if kind.tag != f"{SL}masters-group"
        for el in _paras(kind)
        if el.tag == TABLE or _xml_text(el)
    ]
    attached = {a.get(f"{SFA}ID"): a for a in body.iter(ATTACHMENT)}
    _flow(r, list(_paras(flow)), attached, floating)


def _flow(
    r: _Reader,
    found: list[ET.Element],
    attached: dict[str | None, ET.Element],
    after: list[ET.Element],
) -> None:
    """Paragraphs cited by their place in the flow, empty ones included

    A table takes a number in the count, like the paragraph it stands in for, and one
    anchored in a paragraph shares its number. Elements in `after` continue the count
    """
    width = len(str(len(found)))
    lines: list[tuple[int, str]] = []

    def table(n: int, model: ET.Element) -> None:
        if cells := _xml_table(model):
            _numbered(r, lines, width)
            lines.clear()
            r.out.blocks.append(Block(replace(r.src, line=n), grid(cells)))

    for n, el in enumerate([*found, *after], start=1):
        if el.tag == TABLE:
            table(n, el)
            continue
        if text := _xml_text(el):
            lines.append((n, text))
        for ref in el.iter(ATTACHMENT_REF):
            target = attached.get(ref.get(f"{SFA}IDREF"))
            model = None if target is None else next(target.iter(TABLE), None)
            if model is not None:
                table(n, model)
            elif target is not None:
                lines += [(n, t) for p in target.iter(PARA) if (t := _xml_text(p))]
    _numbered(r, lines, width)


def _preview(bundle: Bundle, src: Src, out: Converted) -> bool:
    for name in (*PREVIEWS, PREVIEW_PDF):
        if not bundle.has(name) or bundle.size(name) > MAX_PREVIEW_BYTES:
            continue
        data = bundle.read(name)
        if name == PREVIEW_PDF:
            out.children.append(Child(name, src, data))
        else:
            out.jobs.append(RecognizeJob("image", src.inside(name), data=data))
        return True
    return False


def _melt(path: Path, z: zipfile.ZipFile | None, src: Src, out: Converted) -> None:
    bundle = _open(path, z)
    if links := bundle.links:
        more = len(links) - SHOW_LINKS
        shown = ", ".join(links[:SHOW_LINKS]) + (f" and {more} more" if more > 0 else "")
        out.needs.append(f"bundle links not followed: {shown}")
    if bundle.has(".iwpv2"):
        _unlock(bundle)
    if path.suffix.lower() == ".numbers":
        if importlib.util.find_spec("numbers_parser") is None:
            out.needs.append("iwork extra (meltify doctor --install iwork)")
        else:
            if bundle.key is None:
                _numbers(path, src, out)
            else:
                _numbers_unlocked(bundle, src, out)
            return
    else:
        legacy = next(
            (
                n
                for n in ("index.xml", "index.xml.gz", "index.apxl", "index.apxl.gz")
                if bundle.has(n)
            ),
            None,
        )
        index = iter(()) if legacy else _index(bundle, out)
        r = _Reader(iwa.objects(index), bundle, src, out)
        r.data = _load_data_names(r.objs)
        if legacy:
            _legacy(bundle, legacy, r)
        elif any(o.type == KN_DOCUMENT for o in r.objs.values()):
            _keynote(r)
        else:
            _pages(r)
        if r.charts:
            out.needs.append(f"iwork skipped {count(r.charts, 'chart')}")
        if r.deep:
            out.needs.append(not_read(r.deep, "iwork drawable", f"nested past {MAX_DEPTH} levels"))
        if out.blocks:
            return
    # No native text, so OCR what the app rendered as a preview
    if _preview(bundle, src, out):
        out.needs.append("iwork preview only")
    else:
        out.needs.append("iwork preview missing")


def sealed(path: Path, head: bytes) -> bool:
    """Whether a password locks the bundle, which keeps its verifier at the root"""
    if path.is_dir():
        return (path / ".iwpv2").exists()
    if not head.startswith(b"PK\x03\x04"):
        return False
    try:
        with zipfile.ZipFile(path) as z:
            # At the root, or under the one folder a zipped bundle may keep
            return any(n.rpartition("/")[2] == ".iwpv2" for n in z.namelist())
    except (OSError, zipfile.BadZipFile):
        return False


def convert(path: Path, src: Src) -> Converted:
    out = Converted("iwork")
    if path.is_dir():
        _melt(path, None, src, out)
    else:
        with zipfile.ZipFile(path) as z:
            _melt(path, z, src, out)
    return out
