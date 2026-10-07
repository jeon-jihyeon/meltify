from __future__ import annotations

import importlib.util
import struct
import zipfile
from dataclasses import dataclass, field, replace
from pathlib import Path

from meltify.converters import Block, Child, Converted, RecognizeJob, iwa
from meltify.converters.iwork_bundle import Bundle, index_files, open_bundle, unlock
from meltify.converters.iwork_legacy import read_legacy, slide_text
from meltify.converters.iwork_tables import (
    TST_INFO,
    TST_MODEL,
    TSWP_STORAGE,
    read_numbers,
    read_numbers_unlocked,
    table_cells,
)
from meltify.converters.tables import grid
from meltify.converters.text import numbered_lines
from meltify.evidence import Src
from meltify.needs import count, not_read

# Newer iWork files keep a full-size preview at the root, older ones under QuickLook
PREVIEWS = ("preview.jpg", "QuickLook/Preview.jpg", "QuickLook/Thumbnail.jpg")
PREVIEW_PDF = "QuickLook/Preview.pdf"
MAX_PREVIEW_BYTES = 256 << 20
RASTER = {".png", ".jpg", ".jpeg", ".gif", ".tif", ".tiff", ".bmp", ".heic", ".webp"}
# Groups nest a few levels in real documents, so a deeper chain is built to blow the stack
MAX_DEPTH = 64
# How many skipped bundle links a needs line names before it just counts the rest
SHOW_LINKS = 5

# Message types, established against real documents by Docling and keynote-parser. Table
# types live with the table reader
KN_DOCUMENT, KN_SHOW, KN_SLIDE_NODE, KN_SLIDE, KN_PLACEHOLDER, KN_NOTE = 1, 2, 4, 5, 7, 15
TP_DOCUMENT = 10000
TSWP_ATTACHMENT, TSWP_NOTE, TSWP_SHAPE = 2003, 2008, 2011
TSD_IMAGE, TSD_GROUP = 3005, 3008
TSCH_CHART = 5000
TSP_PACKAGE = 11006
# Image renditions from best to worst, since Pages doesn't always write all of them
IMAGE_FIELDS = (15, 13, 11, 12)


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
        cells = table_cells(model, self.objs)
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
        if body := slide_text(lines, spoken):
            r.out.blocks.insert(tables, Block(at, body))


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


def _graph(bundle: Bundle, src: Src, out: Converted) -> None:
    """Read an iWork 2013+ document from its object graph"""
    r = _Reader(iwa.objects(index_files(bundle, out)), bundle, src, out)
    r.data = _load_data_names(r.objs)
    if any(o.type == KN_DOCUMENT for o in r.objs.values()):
        _keynote(r)
    else:
        _pages(r)
    if r.charts:
        out.needs.append(f"iwork skipped {count(r.charts, 'chart')}")
    if r.deep:
        out.needs.append(not_read(r.deep, "iwork drawable", f"nested past {MAX_DEPTH} levels"))


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
    bundle = open_bundle(path, z)
    if links := bundle.links:
        more = len(links) - SHOW_LINKS
        shown = ", ".join(links[:SHOW_LINKS]) + (f" and {more} more" if more > 0 else "")
        out.needs.append(f"bundle links not followed: {shown}")
    if bundle.has(".iwpv2"):
        unlock(bundle)
    if path.suffix.lower() == ".numbers":
        if importlib.util.find_spec("numbers_parser") is None:
            out.needs.append("iwork extra (meltify doctor --install iwork)")
        else:
            if bundle.key is None:
                read_numbers(path, src, out)
            else:
                read_numbers_unlocked(bundle, src, out)
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
        if legacy:
            read_legacy(bundle, legacy, src, out)
        else:
            _graph(bundle, src, out)
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
