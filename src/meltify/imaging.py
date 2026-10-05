"""Prepare images so OCR engines can read small or faint text"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

SHARPEN = (0, -1, 0, -1, 5, -1, 0, -1, 0)


def upscale(image: Any, factor: float, sharpen: bool) -> Any:
    from PIL import Image, ImageFilter

    if factor != 1:
        image = image.resize(
            (round(image.width * factor), round(image.height * factor)), Image.Resampling.LANCZOS
        )
    if sharpen:
        image = image.filter(ImageFilter.Kernel((3, 3), SHARPEN, scale=1))
    return image


def equalize(image: Any) -> Any:
    # Spread a narrow band of near-background tones over the full range
    from PIL import ImageOps

    return ImageOps.equalize(image.convert("RGB"))


def render_pdf_page(path: str, page: int, dpi: int) -> Any:
    import pymupdf
    from PIL import Image

    with pymupdf.open(path) as doc:
        pix = doc[page - 1].get_pixmap(dpi=dpi)
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


@dataclass(frozen=True)
class Tile:
    image: Any
    x: int  # offset of the tile in the prepared image
    y: int


def tiles(image: Any, max_side: int, overlap: float = 0.1) -> list[Tile]:
    """Overlapping crops no longer than max_side, so a vision API never downsizes them"""
    if max(image.width, image.height) <= max_side:
        return [Tile(image, 0, 0)]
    step = int(max_side * (1 - overlap))
    out = []
    for y in range(0, max(image.height - int(max_side * overlap), 1), step):
        for x in range(0, max(image.width - int(max_side * overlap), 1), step):
            box = (x, y, min(x + max_side, image.width), min(y + max_side, image.height))
            out.append(Tile(image.crop(box), x, y))
    return out


def dhash(image: Any, size: int = 8) -> int:
    import numpy as np

    gray = np.asarray(image.convert("L").resize((size + 1, size)), dtype=np.int16)
    bits = (gray[:, 1:] > gray[:, :-1]).flatten()
    return int("".join("1" if b else "0" for b in bits), 2)


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


@dataclass(frozen=True)
class Look:
    """Frame fingerprint that tells near-identical frames apart from new ones"""

    shape: int  # dhash of gradients
    tone: float  # mean gray level, since flat frames of different colors share one dhash

    @classmethod
    def of(cls, image: Any) -> Look:
        import numpy as np

        return cls(dhash(image), float(np.asarray(image.convert("L")).mean()))

    def same_as(self, other: Look, distance: int, tone_gap: float = 8.0) -> bool:
        return (
            hamming(self.shape, other.shape) <= distance and abs(self.tone - other.tone) < tone_gap
        )


def save(image: Any, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return path
