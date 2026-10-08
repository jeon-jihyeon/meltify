"""Prepare images so OCR engines can read small or faint text"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from pi_heif import register_heif_opener
except ImportError:  # an older install without the base dependency still reads other formats
    pass
else:
    # Pillow can't decode HEIC on its own, and iPhone photos default to it
    register_heif_opener()

SHARPEN = (0, -1, 0, -1, 5, -1, 0, -1, 0)

# Shorter than this is an icon, a bullet or a tracking pixel, never worth an OCR call
MIN_SIDE = 48
# Engines read small text best once the short side reaches about this many pixels
SHORT_TARGET = 1000
# Past this, upscaling only slows the engines down, and huge scans get scaled down to it
LONG_MAX = 4000
UPSCALE_MAX = 4.0
# Frames read from one image file, enough for a long fax TIFF or a slideshow GIF
MAX_FRAMES = 50
# Formats whose extra frames are pages or animation steps. MPO, ICO and PSD extras repeat
# the same picture at another size or as a layer of the composite
PAGED = {"TIFF", "GIF", "PNG", "WEBP"}
# An animation frame differing from the last kept one in at most this many pixels, by more
# than PIXEL_GAP gray levels, repeats it. One changed digit spans far more pixels
SAME_PIXELS = 16
PIXEL_GAP = 32
# Claude's vision reads up to this many pixels without scaling the picture down again
PICTURE_PIXELS = 1_150_000


def auto_factor(width: int, height: int) -> float:
    """Resize factor that lifts small images toward SHORT_TARGET and never makes them huge"""
    grow = min(max(1.0, SHORT_TARGET / max(1, min(width, height))), UPSCALE_MAX)
    return round(min(grow, LONG_MAX / max(1, width, height)), 3)


def image_size(data: bytes) -> tuple[int, int] | None:
    """Pixel size from the header alone, or None when Pillow can't parse it"""
    import io

    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data)) as im:
            return im.size
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return None


def flatten(image: Any) -> Any:
    """RGB on a white background, so transparent text doesn't turn black"""
    from PIL import Image

    if image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info:
        rgba = image.convert("RGBA")
        bg = Image.new("RGB", rgba.size, "white")
        bg.paste(rgba, mask=rgba.getchannel("A"))
        return bg
    return image.convert("RGB")


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


def distinct_frames(
    paths: Iterable[Path], distance: int, keep_repeats: bool = False
) -> Iterator[tuple[Path, Path | None]]:
    """Each video frame let through, with the earlier frame it repeats or None

    Only the last frame let through counts, so a scene that returns later shows up again.
    Repeats are dropped unless `keep_repeats`
    """
    from PIL import Image

    last: tuple[Look, Path] | None = None
    for path in paths:
        with Image.open(path) as im:
            look = Look.of(im)
        repeats = last[1] if last is not None and look.same_as(last[0], distance) else None
        if repeats is not None and not keep_repeats:
            continue
        last = (look, path)
        yield path, repeats


def frames(path: Path) -> tuple[list[int], int]:
    """Frame numbers worth reading, from 1, and how many frames past MAX_FRAMES go unread

    An animation frame that repeats the last kept one adds nothing, so it's skipped.
    TIFF pages are separate pages of a document and always kept
    """
    import numpy as np
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(path) as im:
            total = getattr(im, "n_frames", 1)
            if total <= 1 or im.format not in PAGED:
                return [1], 0
            kept: list[int] = []
            last = None
            for n in range(total):
                if len(kept) == MAX_FRAMES:
                    return kept, total - n
                im.seek(n)
                if im.format != "TIFF":
                    # Pixels, not a perceptual hash, since a frame may change only one number
                    gray = np.asarray(im.convert("L"), dtype=np.int16)
                    if (
                        last is not None
                        and last.shape == gray.shape
                        and np.count_nonzero(np.abs(gray - last) > PIXEL_GAP) <= SAME_PIXELS
                    ):
                        continue
                    last = gray
                kept.append(n + 1)
            return kept, 0
    except (UnidentifiedImageError, OSError, ValueError, EOFError, Image.DecompressionBombError):
        # Recognition opens it again and reports why it can't
        return [1], 0


def save(image: Any, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return path


def shrink(source: Path | bytes, target: Path, side: int, quality: int, frame: int = 1) -> bool:
    """Save a small WebP of the picture for an agent to look at, False when that fails"""
    import io

    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(source) if isinstance(source, bytes) else source) as im:
            if frame > 1:
                im.seek(frame - 1)
            w, h = im.size
            scale = min(1.0, side / max(w, h), (PICTURE_PIXELS / (w * h)) ** 0.5)
            size = (max(1, int(w * scale)), max(1, int(h * scale)))
            # Shrinking before flatten keeps a huge scan from sitting in memory at full size,
            # and lets JPEG decode at a fraction of it. Palettes only resize by nearest pixel
            if im.mode not in ("P", "1"):
                im.thumbnail(size, Image.Resampling.LANCZOS, reducing_gap=2.0)
            image = flatten(im)
        image.thumbnail(size, Image.Resampling.LANCZOS)
        target.parent.mkdir(parents=True, exist_ok=True)
        # A run cut off mid-write must not leave a broken file a rerun would reuse
        partial = target.with_name(f"{target.name}.part")
        image.save(partial, "WEBP", quality=quality)
        partial.replace(target)
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        EOFError,
        MemoryError,
        Image.DecompressionBombError,
    ):
        return False
    return True
