from __future__ import annotations

from pathlib import Path

from meltify.converters import Converted
from meltify.converters.text import numbered
from meltify.evidence import Src


def convert(path: Path, src: Src) -> Converted:
    from striprtf.striprtf import rtf_to_text

    # RTF is 7-bit text, and latin-1 keeps any stray 8-bit byte instead of failing on it.
    # striprtf then decodes \'xx bytes with the file's own \ansicpg or font charset
    raw = path.read_bytes().decode("latin-1")
    text = rtf_to_text(raw, errors="replace")
    return Converted("rtf", numbered(text, src))
