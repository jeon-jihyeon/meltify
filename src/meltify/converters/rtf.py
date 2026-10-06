from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from meltify.converters import Converted
from meltify.converters.embeds import Embeds
from meltify.converters.text import numbered
from meltify.evidence import Src

# Picture kinds keyed by their RTF control word, as the file suffix they'd be saved under
PICTURES = {"pngblip": ".png", "jpegblip": ".jpg", "emfblip": ".emf", "wmetafile": ".wmf"}
# Pictures nothing here decodes, named the way a needs entry reports them
OTHER = {"macpict": "pict", "pmmetafile": "os2 metafile", "dibitmap": "dib", "wbitmap": "bitmap"}
KINDS = PICTURES.keys() | OTHER.keys()
# \binN carries N raw bytes, so it's matched apart from other control words
TOKEN = re.compile(r"\\bin(\d+) ?|\\([a-zA-Z]+)-?\d* ?|\\.|[{}]|[^\\{}]+", re.S)


@dataclass
class Pict:
    depth: int
    para: int
    start: int  # offset of the group's opening brace, so the text can leave it out
    copy: bool = False
    end: int = 0
    kind: str | None = None
    chunks: list[str] = field(default_factory=list)
    binary: bytes = b""

    def data(self) -> bytes | None:
        if self.binary:
            return self.binary
        try:
            return bytes.fromhex(re.sub(r"\s+", "", "".join(self.chunks)))
        except ValueError:
            return None


def picts(raw: str) -> list[Pict]:
    """Every \\pict group in reading order, with the paragraph it sits in"""
    found: list[Pict] = []
    opens: list[int] = []
    para, pos = 1, 0
    pict: Pict | None = None
    # Word follows each picture with a \nonshppict copy for old readers
    copy: int | None = None
    while m := TOKEN.match(raw, pos):
        pos = m.end()
        token, word = m.group(0), m.group(2)
        here = pict is not None and len(opens) == pict.depth
        if token == "{":
            opens.append(m.start())
        elif token == "}":
            if here:
                pict.end = pos
                found.append(pict)
                pict = None
            if copy == len(opens):
                copy = None
            if opens:
                opens.pop()
        elif m.group(1) is not None:
            size = int(m.group(1))
            if here:
                pict.binary += raw[pos : pos + size].encode("latin-1")
            pos += size
        elif word == "nonshppict":
            copy = len(opens)
        elif word == "pict" and opens and pict is None:
            pict = Pict(len(opens), para, opens[-1], copy=copy is not None)
        elif here and word in KINDS:
            pict.kind = word
        elif word == "par" and pict is None:
            para += 1
        elif here and not token.startswith("\\"):
            pict.chunks.append(token)
    if pict is not None:
        # A cut-off file still reports the picture it was in the middle of
        pict.end = len(raw)
        found.append(pict)
    return found


def images(found: list[Pict], src: Src) -> Embeds:
    embeds = Embeds()
    for i, p in enumerate((p for p in found if not p.copy), start=1):
        data = p.data()
        if not data:
            embeds.skipped["unreadable image"] += 1
        elif p.kind in PICTURES:
            # EMF and WMF give their text records, then are drawn or listed as needs
            embeds.picture(replace(src, para=p.para, img=i), data, f"picture{PICTURES[p.kind]}")
        else:
            embeds.skipped[f"{OTHER.get(p.kind or '', 'unknown')} image"] += 1
    return embeds


def convert(path: Path, src: Src) -> Converted:
    from striprtf.striprtf import rtf_to_text

    # RTF is 7-bit text, and latin-1 keeps any stray 8-bit byte instead of failing on it.
    # striprtf then decodes \'xx bytes with the file's own \ansicpg or font charset
    raw = path.read_bytes().decode("latin-1")
    found = picts(raw)
    # Picture data is no text, and raw \bin bytes would throw striprtf's brace count off
    kept, last = [], 0
    for p in found:
        kept.append(raw[last : p.start])
        last = p.end
    text = rtf_to_text("".join(kept) + raw[last:], errors="replace")
    out = Converted("rtf", numbered(text, src))
    images(found, src).into(out)
    if not (out.blocks or out.jobs or out.children):
        # An empty file, or one cut off before its first paragraph, still has to show up
        out.needs.append("rtf has no text")
    return out
