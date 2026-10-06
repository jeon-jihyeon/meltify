"""Size and inflate limits for what containers unpack, so a crafted file can't fill memory"""

from __future__ import annotations

import zipfile

# A part of an Office, EPUB or HWPX package bigger than this is more likely a zip bomb
# than a picture or document part worth reading
MAX_PART_BYTES = 64 << 20
# One archive member, ODF content part or iWork bundle file
MAX_MEMBER_BYTES = 256 << 20
# A member that inflates past this ratio is a zip bomb, once it's big enough to matter.
# Text compresses well, so smaller members never count
MAX_RATIO = 100
RATIO_FLOOR = 1 << 20
# What one HWPX document may unpack for its parts in all, and per byte of the document,
# with a floor that always fits one part at the part limit
MAX_EMBED_BYTES = 512 << 20
EMBED_RATIO = 100
EMBED_FLOOR_BYTES = 64 << 20


def human_bytes(n: int) -> str:
    """A byte count the way a limit reads in needs, like `256 MiB`"""
    for unit, shift in (("GiB", 30), ("MiB", 20)):
        if n >= 1 << shift:
            return f"{n / (1 << shift):g} {unit}"
    return f"{n} bytes"


def inflates(size: int, packed: int | None) -> bool:
    """Whether a member inflating from `packed` to `size` bytes looks like a zip bomb

    None for `packed` means the format stores no compressed size, so there's no ratio
    """
    return packed is not None and size > RATIO_FLOOR and size > packed * MAX_RATIO


def checked(info: zipfile.ZipInfo) -> zipfile.ZipInfo:
    """The member, once its declared sizes pass the part size and inflate limits"""
    if info.file_size > MAX_PART_BYTES:
        raise ValueError(f"{info.filename} is over the {human_bytes(MAX_PART_BYTES)} part limit")
    if inflates(info.file_size, info.compress_size):
        raise ValueError(f"{info.filename} expands over {MAX_RATIO}:1")
    return info


def read_part(z: zipfile.ZipFile, part: str) -> bytes:
    """A member's bytes, refused before reading past the part size and inflate limits"""
    return z.read(checked(z.getinfo(part)))


class EmbedBudget:
    """Bytes one HWPX document may unpack while its parts are decrypted into a new package

    The rebuilt package sits in memory, and a small file whose parts inflate would
    otherwise fill it. The cap is a total, and a ratio to the document's own size with a
    floor that always fits one ordinary part
    """

    def __init__(self, size: int) -> None:
        self.left = min(MAX_EMBED_BYTES, max(size * EMBED_RATIO, EMBED_FLOOR_BYTES))

    def spend(self, n: int) -> bool:
        """Take `n` bytes, or nothing and False once they don't fit"""
        if n > self.left:
            return False
        self.left -= n
        return True
