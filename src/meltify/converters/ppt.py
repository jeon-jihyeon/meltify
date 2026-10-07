"""PowerPoint 97 to 2003 binaries: slide text and pictures straight from the records

Templates and slideshows (.pot, .pps) share the deck's format. LibreOffice steps in for
PowerPoint 95, decks the records can't be followed in, decks read without olefile, and
slides with no text or pictures to OCR
"""

from __future__ import annotations

import importlib.util
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from meltify.converters import Block, Converted, RecognizeJob, run
from meltify.converters.blips import (
    PICTURE_ENTRY,
    PICTURE_PROPS,
    SHAPE_OPTIONS,
    Blips,
    Picture,
    RecordError,
    atoms,
    children,
    entry,
    header,
    place,
)
from meltify.converters.embeds import SHALLOW, Embeds
from meltify.converters.render import soffice
from meltify.evidence import Src
from meltify.needs import LIBREOFFICE, count, error_note
from meltify.passwords import LOCKED, NEEDS_CRYPTO, Locked, cant_decrypt
from meltify.safe import MissingTool

OFFICE_HINT = "meltify doctor --install office"
SOFFICE_TIMEOUT = 180

# MS-PPT record types
CURRENT_USER = 0x0FF6
USER_EDIT = 0x0FF5
PERSIST_DIRECTORY = 0x1772
DOCUMENT = 0x03E8
SLIDE_LIST = 0x0FF0
SLIDE_PERSIST = 0x03F3
SLIDE = 0x03EE
NOTES = 0x03F0
NOTES_ATOM = 0x03F1
SLIDE_SHOW_INFO = 0x03F9
TEXT_CHARS = 0x0FA0
TEXT_BYTES = 0x0FA8
OUTLINE_REF = 0x0F9E
DRAWING_GROUP = 0x040B
# Current User header tokens
PLAIN = 0xE391C05F
ENCRYPTED = 0xF3D1C4DF
# What PowerPoint writes for date and slide number fields, not text anyone typed
FIELD = "*"


class PptError(RecordError):
    """A PowerPoint Document stream this parser can't follow"""


class OldPpt(PptError):
    """PowerPoint 95 and older, a different record layout"""


@dataclass
class _Slide:
    texts: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    pictures: list[int] = field(default_factory=list)  # 1-based picture store indices
    hidden: bool = False


@dataclass
class _Deck:
    slides: list[_Slide]
    # Picture store in order, None for an entry that holds no picture
    pictures: list[Picture]
    # Notes pages whose slide is gone, which still hold text someone wrote
    loose: list[str]
    # Holds the store's pictures, so they count against one total as they unpack
    embeds: Embeds


def _text(data: bytes, kind: int, body: int, end: int) -> str:
    raw = data[body:end]
    # Bytes atoms hold the low byte of UTF-16 code units whose high byte is zero
    text = raw.decode("utf-16-le", "replace") if kind == TEXT_CHARS else raw.decode("latin-1")
    text = text.replace("\r", "\n").replace("\x0b", "\n")
    return "".join(c for c in text if c >= " " or c in "\n\t").strip()


def _persist(doc: bytes, offset: int) -> tuple[dict[int, int], int]:
    """Persist ids mapped to stream offsets, and the document's persist id

    Every save appends a user edit with its own directory, so the newest one wins
    """
    where: dict[int, int] = {}
    seen: set[int] = set()
    document = None
    while offset not in seen:
        seen.add(offset)
        kind, _, body, end = header(doc, offset)
        if kind != USER_EDIT or end - body < 28:
            raise PptError("broken edit chain")
        last, directory, ref = struct.unpack_from("<III", doc, body + 8)
        if document is None:
            document = ref
        if end - body >= 32 and struct.unpack_from("<I", doc, body + 28)[0]:
            raise Locked(LOCKED)
        kind, _, pos, stop = header(doc, directory)
        if kind != PERSIST_DIRECTORY:
            raise PptError("missing persist directory")
        while pos + 4 <= stop:
            word = struct.unpack_from("<I", doc, pos)[0]
            first, n = word & 0xFFFFF, word >> 20
            pos += 4
            for i in range(n):
                if pos + 4 > stop:
                    break
                where.setdefault(first + i, struct.unpack_from("<I", doc, pos)[0])
                pos += 4
        if last == 0:
            break
        offset = last
    if document is None or document not in where:
        raise PptError("no document container")
    return where, document


def _container(doc: bytes, where: dict[int, int], ref: int, kind: int) -> tuple[int, int] | None:
    if ref not in where:
        return None
    found, _, body, end = header(doc, where[ref])
    return (body, end) if found == kind else None


def _lists(doc: bytes, body: int, end: int) -> dict[int, list[tuple[int, int, list[str]]]]:
    """Slide lists by instance, each entry its persist id, slide id and outline texts

    Instance 0 lists the slides in show order and instance 2 their notes
    """
    lists: dict[int, list[tuple[int, int, list[str]]]] = {}
    for kind, inst, start, stop in children(doc, body, end):
        if kind != SLIDE_LIST:
            continue
        entries = lists.setdefault(inst, [])
        for atom, _, a, b in children(doc, start, stop):
            if atom == SLIDE_PERSIST and b - a >= 16:
                ref, _, _, slide_id = struct.unpack_from("<IIiI", doc, a)
                entries.append((ref, slide_id, []))
            elif atom in (TEXT_CHARS, TEXT_BYTES) and entries:
                entries[-1][2].append(_text(doc, atom, a, b))
    return lists


def _texts(doc: bytes, body: int, end: int, outline: list[str], slide: _Slide) -> list[str]:
    """Text of one slide or notes page in drawing order

    Placeholders point into the slide list's outline texts, while text boxes keep
    their own. Outline texts nothing pointed at go last, unless a shape repeats them
    """
    found: list[str] = []
    used: set[int] = set()
    for kind, inst, a, b in atoms(doc, body, end):
        if kind in (TEXT_CHARS, TEXT_BYTES):
            found.append(_text(doc, kind, a, b))
        elif kind == OUTLINE_REF and b - a >= 4:
            i = struct.unpack_from("<i", doc, a)[0]
            if 0 <= i < len(outline):
                used.add(i)
                found.append(outline[i])
        elif kind == SHAPE_OPTIONS:
            # The instance counts the fixed properties, and complex data follows them
            for k in range(min(inst, (b - a) // 6)):
                pid, value = struct.unpack_from("<HI", doc, a + 6 * k)
                # The fBid bit marks a value that is a picture store index
                if pid & 0x3FFF in PICTURE_PROPS and pid & 0x4000 and value:
                    slide.pictures.append(value)
        elif kind == SLIDE_SHOW_INFO and b - a >= 12:
            slide.hidden = bool(struct.unpack_from("<H", doc, a + 10)[0] & 0x4)
    found += [t for i, t in enumerate(outline) if i not in used and t not in found]
    return [t for t in found if t and t != FIELD]


def _store(doc: bytes, body: int, end: int, pictures: bytes, blips: Blips) -> list[Picture]:
    """The deck's picture store, read through the drawing group's entries"""
    store: list[Picture] = []
    for kind, _, start, stop in children(doc, body, end):
        if kind != DRAWING_GROUP:
            continue
        for atom, _, a, b in atoms(doc, start, stop):
            if atom != PICTURE_ENTRY:
                continue
            store.append(entry(doc, a, b, pictures, blips))
    return store


def _edit_offset(ole: Any) -> int:
    """Where the newest user edit starts, from the Current User stream

    Raises OldPpt for PowerPoint 95, whose records this parser doesn't read, and Locked
    for an encrypted deck
    """
    if not ole.exists("PowerPoint Document"):
        if ole.exists("PP40"):
            raise OldPpt("PowerPoint 95")
        raise PptError("no PowerPoint Document stream")
    user = ole.openstream("Current User").read() if ole.exists("Current User") else b""
    if len(user) >= 4 and struct.unpack_from("<I", user)[0] + 4 == len(user):
        raise OldPpt("PowerPoint 95")
    if len(user) < 20:
        raise PptError("no Current User stream")
    kind, _, body, _ = header(user, 0)
    token, offset = struct.unpack_from("<II", user, body + 4)
    if kind != CURRENT_USER or token not in (PLAIN, ENCRYPTED):
        raise PptError("unknown Current User record")
    if token == ENCRYPTED:
        raise Locked(LOCKED)
    return offset


def _slides(
    doc: bytes, where: dict[int, int], lists: dict[int, list[tuple[int, int, list[str]]]]
) -> tuple[list[_Slide], list[str]]:
    """Slides in show order with their notes, and notes whose slide is gone"""
    slides: list[_Slide] = []
    by_id: dict[int, _Slide] = {}
    loose: list[str] = []
    for persist, slide_id, outline in lists.get(0, []):
        slide = _Slide()
        at = _container(doc, where, persist, SLIDE)
        if at is not None:
            slide.texts = _texts(doc, *at, outline, slide)
        else:
            slide.texts = [t for t in outline if t and t != FIELD]
        slides.append(slide)
        by_id[slide_id] = slide
    for persist, _, outline in lists.get(2, []):
        at = _container(doc, where, persist, NOTES)
        if at is None:
            continue
        texts = _texts(doc, *at, outline, _Slide())
        owner = next(
            (
                by_id.get(struct.unpack_from("<I", doc, a)[0])
                for kind, _, a, b in children(doc, *at)
                if kind == NOTES_ATOM and b - a >= 4
            ),
            None,
        )
        if owner is not None:
            owner.notes += texts
        else:
            loose += texts
    return slides, loose


def _deck(ole: Any) -> _Deck:
    offset = _edit_offset(ole)
    doc = ole.openstream("PowerPoint Document").read()
    pictures = ole.openstream("Pictures").read() if ole.exists("Pictures") else b""
    where, ref = _persist(doc, offset)
    found = _container(doc, where, ref, DOCUMENT)
    if found is None:
        raise PptError("no document container")
    slides, loose = _slides(doc, where, _lists(doc, *found))
    embeds = Embeds()
    return _Deck(slides, _store(doc, *found, pictures, Blips(embeds)), loose, embeds)


def _unlocked_deck(path: Path) -> tuple[Path, _Deck]:
    """A decrypted copy of the deck and what it holds, for a ppt read hasn't decrypted first

    Decryption and wrong password checks go through unlock, the one path every encrypted
    Office file takes. Raises Locked when the deck stays shut
    """
    from meltify.converters.unlock import unlock

    if importlib.util.find_spec("msoffcrypto") is None:
        raise Locked(NEEDS_CRYPTO)
    opened = unlock(path)
    if opened == path:
        raise Locked(cant_decrypt("msoffcrypto doesn't see the encryption"))
    try:
        return opened, _open_deck(opened)
    except (RecordError, OSError, struct.error, Locked) as e:
        shutil.rmtree(opened.parent, ignore_errors=True)
        raise Locked(cant_decrypt(error_note(e))) from e


def _open_deck(source: Path) -> _Deck:
    import olefile

    with olefile.OleFileIO(source) as ole:
        return _deck(ole)


def _not_read(n: int, what: str, why: str) -> str:
    return f"{count(n, what)} without text not read ({why})"


def _empty_slides(path: Path, src: Src, deck: _Deck, empty: list[int]) -> Converted:
    """OCR jobs for slides with no text or pictures, taken from a LibreOffice render

    The PDF leaves hidden slides out, so pages count only the shown ones
    """
    from meltify.converters import render

    out = Converted("legacy")
    pages, page = {}, 0
    for n, slide in enumerate(deck.slides, start=1):
        page += not slide.hidden
        if n in empty and not slide.hidden:
            pages[n] = page
    if hidden := len(empty) - len(pages):
        out.needs.append(_not_read(hidden, "hidden slide", "a PDF render leaves it out"))
    if not pages:
        return out
    if soffice() is None:
        out.needs.append(_not_read(len(pages), "slide", LIBREOFFICE))
        return out
    if run.current().shallow:
        # The render only feeds OCR, which --shallow skips
        out.needs.append(_not_read(len(pages), "slide", SHALLOW))
        return out
    missed = 0
    with tempfile.TemporaryDirectory(prefix="meltify-ppt-") as tmp:
        try:
            pdf = render.soffice_pdf(path, Path(tmp), SOFFICE_TIMEOUT)
        except (OSError, RuntimeError, subprocess.SubprocessError):
            pdf = None
        for n, p in pages.items():
            try:
                image = None if pdf is None else render.png(pdf, p)
            except Exception:  # noqa: BLE001
                # A page the render doesn't have stays listed as not read
                image = None
            if image is None:
                missed += 1
            else:
                out.jobs.append(RecognizeJob("image", replace(src, slide=n), data=image))
    if missed:
        out.needs.append(_not_read(missed, "slide", "LibreOffice could not render it"))
    return out


def _native(path: Path, src: Src, deck: _Deck) -> Converted:
    out = Converted("legacy")
    for n, slide in enumerate(deck.slides, start=1):
        text = "\n".join(slide.texts)
        if slide.notes:
            text = (text + "\n\n" if text else "") + "Notes:\n" + "\n".join(slide.notes)
        if text:
            out.blocks.append(Block(replace(src, slide=n), text))
    if deck.loose:
        out.blocks.append(Block(src, "Notes:\n" + "\n".join(deck.loose)))
    embeds = deck.embeds
    drawn: set[int] = set()

    def picture(at: Src, index: int) -> None:
        found = deck.pictures[index - 1] if 0 < index <= len(deck.pictures) else None
        place(embeds, at, found, index)

    for n, slide in enumerate(deck.slides, start=1):
        i = 0
        for index in slide.pictures:
            # The same picture on many slides, like a logo, holds the same text
            if index in drawn:
                continue
            drawn.add(index)
            i += 1
            picture(replace(src, slide=n, img=i), index)
    # Pictures only masters or notes use, numbered by their place in the store
    for index, found in enumerate(deck.pictures, start=1):
        if found is not None and index not in drawn:
            picture(replace(src, img=index), index)
    empty = [n for n, s in enumerate(deck.slides, start=1) if not s.texts and not s.pictures]
    if empty:
        rendered = _empty_slides(path, src, deck, empty)
        out.jobs += rendered.jobs
        out.needs += rendered.needs
    embeds.into(out)
    return out


def convert(path: Path, src: Src) -> Converted:
    """Slide text and pictures straight from the binary records, LibreOffice only as a fallback"""
    try:
        import olefile  # noqa: F401
    except ImportError:
        if soffice() is None:
            raise MissingTool("olefile", OFFICE_HINT) from None
        return _rendered(path, src)
    try:
        return _native(path, src, _open_deck(path))
    except OldPpt:
        if soffice() is not None:
            return _rendered(path, src)
        return Converted("legacy", needs=[f"PowerPoint 95 ppt not read ({LIBREOFFICE})"])
    except Locked:
        opened, deck = _unlocked_deck(path)
        try:
            return _native(opened, src, deck)
        finally:
            shutil.rmtree(opened.parent, ignore_errors=True)
    except (RecordError, OSError, struct.error) as e:
        failed = error_note(e)
        if soffice() is not None:
            return _rendered(path, src)
        return Converted("legacy", needs=[f"ppt ({failed}, {LIBREOFFICE} to retry)"])


def _rendered(path: Path, src: Src) -> Converted:
    from meltify.converters import render

    # Recorded as drawn from the ppt, so its image-only pages hit the OCR cache next run
    out = render.read_rendered(
        path, src, lambda p, d: render.soffice_pdf(p, d, SOFFICE_TIMEOUT), "legacy", "meltify-ppt-"
    )
    # soffice_pdf raises instead of drawing nothing
    assert out is not None
    return out
