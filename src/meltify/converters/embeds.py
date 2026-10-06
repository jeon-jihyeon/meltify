"""Pictures, charts and diagrams a document embeds, held under one picture total"""

from __future__ import annotations

import hashlib
import posixpath
import subprocess
import tempfile
import zipfile
import zlib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from meltify.converters import Block, Child, Converted, RecognizeJob, place
from meltify.converters.limits import MAX_PART_BYTES, MAX_RATIO, human_bytes, inflates
from meltify.evidence import Src
from meltify.needs import not_read

# What one document's pictures may hold in memory together, however often they repeat
MAX_PICTURE_BYTES = 256 << 20
# Skipped keys for pictures left unread, as "what (why)"
OVER_TOTAL = f"image (over the {human_bytes(MAX_PICTURE_BYTES)} picture total)"
INFLATED = f"image (expands over {MAX_RATIO}:1)"
# Pillow can't draw these, so they stay listed as needs instead of failing the file
VECTOR = {".emf", ".wmf", ".emz", ".wmz", ".svg"}
# The gzipped forms, read once they're unpacked
UNZIPPED = {".emz": ".emf", ".wmz": ".wmf"}
# What draws EMF and WMF pictures for OCR when their text records don't cover them
DRAW_HINT = "install LibreOffice, or metafile-render with meltify doctor --install office"
# Why a page or picture drawn only for OCR stays undrawn, since --shallow skips OCR
SHALLOW = "not drawn under --shallow"


def gunzip(data: bytes) -> bytes | None:
    """The unpacked bytes, None when they're corrupt or unpack past the part limit"""
    try:
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        out = d.decompress(data, MAX_PART_BYTES)
    except zlib.error:
        return None
    return None if d.unconsumed_tail else out


@dataclass
class Embeds:
    """Pictures, charts and diagrams found inside a document, and the ones it can't read

    Every picture goes through `admit`, so one document's pictures share MAX_PICTURE_BYTES
    and identical bytes are held once, however many places show them. Zip-based converters
    read members through `load`, which also reads each member once
    """

    jobs: list[RecognizeJob] = field(default_factory=list)
    # Pictures left unread by what they are, or "what (why)" for a reason in needs
    skipped: Counter[str] = field(default_factory=Counter)
    blocks: list[Block] = field(default_factory=list)
    children: list[Child] = field(default_factory=list)
    # EMF and WMF pictures held back so one render call draws them all
    vectors: list[tuple[Src, bytes, str]] = field(default_factory=list)
    # Charts that saved no values to read
    uncached: list[Src] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # Picture bytes held so far, counted once for each distinct picture
    held: int = 0
    # The held pictures by digest, so a repeat shares the first copy
    _digests: dict[bytes, bytes] = field(default_factory=dict)
    # The held copies by identity, so handing one back again skips hashing it
    _ids: dict[int, bytes] = field(default_factory=dict)
    # Members already loaded by name, or the skipped key that kept one out
    _members: dict[str, bytes | str] = field(default_factory=dict)

    def admit(self, data: bytes) -> bytes | None:
        """The one held copy of these bytes

        None once the document's pictures would pass MAX_PICTURE_BYTES, counted in needs
        """
        if (kept := self.hold(data)) is None:
            self.skipped[OVER_TOTAL] += 1
        return kept

    def hold(self, data: bytes) -> bytes | None:
        """Like admit, but a refusal is left for the caller to list"""
        if self._ids.get(id(data)) is data:
            return data
        digest = hashlib.blake2b(data, digest_size=16).digest()
        if (same := self._digests.get(digest)) is not None:
            return same
        if self.held + len(data) > MAX_PICTURE_BYTES:
            return None
        self.held += len(data)
        self._digests[digest] = data
        self._ids[id(data)] = data
        return data

    def refusal(self, size: int, packed: int | None = None) -> str | None:
        """The skipped key that keeps out a picture of `size` bytes, checked before reading it

        `packed` is what the picture inflates from, when it comes out of compressed data
        """
        if size > MAX_PART_BYTES:
            return "oversized image"
        if inflates(size, packed):
            return INFLATED
        if self.held + size > MAX_PICTURE_BYTES:
            return OVER_TOTAL
        return None

    def load(self, z: zipfile.ZipFile, name: str) -> bytes | None:
        """A picture member's bytes, read once however often the document refers to it

        None when it's missing, too big, inflates like a zip bomb or doesn't fit the
        picture total, each counted in needs for every place that refers to it
        """
        if isinstance(found := self._members.get(name), bytes):
            return found
        if found is None:
            found = self._read(z, name)
            self._members[name] = found
        elif found:
            self.skipped[found] += 1
        return found if isinstance(found, bytes) else None

    def _read(self, z: zipfile.ZipFile, name: str) -> bytes | str:
        try:
            info = z.getinfo(name)
        except KeyError:
            why = "missing image"
        else:
            why = self.refusal(info.file_size, info.compress_size)
            if why is None and (data := self.hold(z.read(info))) is not None:
                return data
            why = why or OVER_TOTAL
        self.skipped[why] += 1
        return why

    def add(self, src: Src, data: bytes, name: str = "") -> None:
        if (kept := self.admit(data)) is not None:
            self._queue(src, kept, name)

    def _queue(self, src: Src, data: bytes, name: str) -> None:
        from meltify import imaging

        if posixpath.splitext(name)[1].lower() in VECTOR:
            self.skipped[f"{posixpath.splitext(name)[1][1:].lower()} image"] += 1
            return
        size = imaging.image_size(data)
        if size is None:
            self.skipped["unreadable image"] += 1
        elif min(size) >= imaging.MIN_SIDE:
            self.jobs.append(RecognizeJob("image", src, data=data))

    def picture(self, src: Src, data: bytes, name: str) -> None:
        """Like add, but SVG goes on to the svg converter and EMF or WMF is held for `draw`"""
        ext = posixpath.splitext(name)[1].lower()
        if ext in UNZIPPED:
            unpacked = gunzip(data)
            if unpacked is None:
                self.skipped["unreadable image"] += 1
                return
            data, name, ext = unpacked, posixpath.splitext(name)[0] + UNZIPPED[ext], UNZIPPED[ext]
        if (kept := self.admit(data)) is None:
            return
        data = kept
        if ext == ".svg":
            # The same picture drawn twice holds the same text, while different ones can
            # share a name, like every inline SVG on a web page
            if all(c.data is not data for c in self.children):
                self.children.append(Child(name, src, data))
        elif ext in VECTOR:
            self.vectors.append((src, data, ext))
        else:
            self._queue(src, data, name)

    def member(
        self, z: zipfile.ZipFile, src: Src, targets: dict[str, str | None], rid: str | None
    ) -> None:
        if rid in targets and targets[rid] is None:
            self.skipped["linked image"] += 1
            return
        name = targets.get(rid or "") or ""
        if (data := self.load(z, name)) is not None:
            self.picture(src, data, name)

    def draw(self) -> None:
        """Read the held EMF and WMF pictures, from their text records and then their pixels

        Text records alone cover a picture unless it also holds bitmaps or text they can't
        decode, so only those pictures and ones without text records are drawn for OCR
        """
        from meltify.converters import metafile

        if not self.vectors:
            return
        held, self.vectors = self.vectors, []
        # What a drawing would add to each picture, named the way a needs entry lists it
        missing: dict[int, list[str]] = {}
        pixels: list[tuple[Src, bytes, str]] = []
        for src, data, ext in held:
            found = metafile.scan(data)
            if found is None or not found.lines:
                missing[len(pixels)] = [f"{ext[1:]} image"]
                pixels.append((src, data, ext))
                continue
            self.blocks.append(Block(src, found.text))
            left = [f"{ext[1:]} {what}" for what in found.undecoded]
            if found.bitmaps:
                left.append(f"{ext[1:]} embedded bitmap")
            if left:
                missing[len(pixels)] = left
                pixels.append((src, data, ext))
        undrawn, why = self._pixels(pixels)
        for what, n in Counter(w for i in undrawn for w in missing[i]).items():
            self.notes.append(not_read(n, what, why))

    def _pixels(self, held: list[tuple[Src, bytes, str]]) -> tuple[list[int], str]:
        """Queue OCR for drawings of the held pictures, from render's backends or metafile-render

        Returns the indexes of the pictures nothing drew, and why
        """
        from meltify.converters import metafile, quicklook, render, run

        left = list(range(len(held)))
        shallow = left and run.current().shallow
        drawable = render.soffice() or quicklook.enabled() or metafile.replay_available()
        if shallow and drawable:
            # A drawing only feeds OCR, so neither a renderer nor a replay starts. Without
            # either, the hint to install one below still applies
            return left, SHALLOW
        tried = False
        if left and render.available():
            tried = True
            left = self._render(held, left)
        if left and metafile.replay_available():
            tried = True
            undrawn = []
            for i in left:
                src, data, _ = held[i]
                if (image := metafile.replay(data)) is None:
                    undrawn.append(i)
                else:
                    self.add(src, image, "render.png")
            left = undrawn
        return left, "no renderer could draw it" if tried else DRAW_HINT

    def _render(self, held: list[tuple[Src, bytes, str]], wanted: list[int]) -> list[int]:
        """Draw the wanted pictures in one render call, returning the ones it couldn't"""
        from meltify.converters import render

        undrawn = []
        with tempfile.TemporaryDirectory(prefix="meltify-vector-") as tmp:
            files = {i: Path(tmp) / f"v{i}{held[i][2]}" for i in wanted}
            for i, f in files.items():
                f.write_bytes(held[i][1])
            try:
                timeout = render.TIMEOUT + 5 * len(files)
                pdfs = render.to_pdfs(list(files.values()), Path(tmp) / "pdf", timeout)
            except (OSError, RuntimeError, subprocess.SubprocessError):
                pdfs = {}
            for i, f in files.items():
                try:
                    self.add(held[i][0], render.png(pdfs[f]), "render.png")
                except Exception:  # noqa: BLE001
                    # Not in pdfs, or a page PyMuPDF can't draw
                    undrawn.append(i)
        return undrawn

    def needs(self) -> list[str]:
        found = []
        for key, n in self.skipped.items():
            if not n:
                continue
            what, _, why = key.partition(" (")
            found.append(not_read(n, what, why.removesuffix(")")))
        if self.uncached:
            found.append(not_read(len(self.uncached), "chart", "no cached values"))
        return found + self.notes

    def into(self, out: Converted) -> None:
        self.draw()
        # Charts and diagrams sit with their slide, sheet or paragraph, not after all the text
        out.blocks = place(out.blocks, self.blocks)
        out.children += self.children
        out.jobs += self.jobs
        out.needs += self.needs()
