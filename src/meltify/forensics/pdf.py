"""Text a PDF carries but a reader doesn't see

Signals per text span:
1. Render mode 3, which never paints glyphs
2. Opacity near zero
3. A font size too small to read
4. A box outside the page
5. Membership in an optional content layer
6. A color close to the fill behind it, or to white when nothing is behind it
7. A filled path, image or shading painted over it later

Limits:
1. Text drawn inside an image is pixels, so only rendering and OCR can find it
2. A background counts only when one fill fully contains the span
3. Text over an image skips the color check, since the pixels aren't sampled
"""

from __future__ import annotations

import functools
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

COVERING = {"fill-path", "fill-image", "fill-shade"}


def luminance(color: Any) -> float:
    c = tuple(color or (0,))
    if len(c) == 1:
        return float(c[0])
    if len(c) == 4:
        # Approximate CMYK through RGB
        k = c[3]
        c = ((1 - c[0]) * (1 - k), (1 - c[1]) * (1 - k), (1 - c[2]) * (1 - k))
    return 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]


@dataclass(frozen=True)
class Thresholds:
    opacity: float = 0.05
    size: float = 4.0
    contrast: float = 0.12


@dataclass(frozen=True)
class HiddenSpan:
    page: int
    text: str
    reasons: tuple[str, ...]
    bbox: tuple[float, float, float, float]
    color: float
    background: float
    size: float
    opacity: float


class Marks:
    """What one page draws, collected once for every check that reads it

    Image and covering boxes sit in arrays by their place in the draw log, so testing a
    span against all of them is one vector operation instead of a pass over the log
    """

    def __init__(self, page: Any) -> None:
        self.page = page

    @functools.cached_property
    def trace(self) -> list[dict[str, Any]]:
        return self.page.get_texttrace()

    @functools.cached_property
    def _log(self) -> list[Any]:
        return self.page.get_bboxlog()

    @functools.cached_property
    def images(self) -> Boxes:
        return Boxes.of(self._log, {"fill-image"})

    @functools.cached_property
    def covers(self) -> Boxes:
        return Boxes.of(self._log, COVERING)


class Boxes:
    """Draw log boxes as columns of their log index and corners"""

    def __init__(self, rows: list[tuple[float, ...]]) -> None:
        import numpy as np

        self.seq, self.x0, self.y0, self.x1, self.y1 = np.array(rows, float).reshape(-1, 5).T
        # pymupdf never counts an empty or infinite rect as intersecting anything
        self.solid = (self.x0 < self.x1) & (self.y0 < self.y1) & ~self._infinite()

    @classmethod
    def of(cls, log: list[Any], kinds: set[str]) -> Boxes:
        return cls([(q, *box) for q, (kind, box, *_) in enumerate(log) if kind in kinds])

    def _infinite(self) -> Any:
        import pymupdf

        low, high = pymupdf.FZ_MIN_INF_RECT, pymupdf.FZ_MAX_INF_RECT
        return (self.x0 == low) & (self.y0 == low) & (self.x1 == high) & (self.y1 == high)

    def contain(self, rect: Any) -> Any:
        """Which boxes contain `rect`, the way pymupdf's Rect contains test decides"""
        if not (rect.x0 <= rect.x1 and rect.y0 <= rect.y1):
            return self.seq < 0
        return (
            (self.x0 <= rect.x0)
            & (rect.x1 <= self.x1)
            & (self.y0 <= rect.y0)
            & (rect.y1 <= self.y1)
        )

    def touch(self, rect: Any) -> Any:
        """Which boxes intersect `rect`, the way pymupdf's Rect intersects test decides"""
        if rect.is_empty or rect.is_infinite:
            return self.seq < 0
        return (
            self.solid
            & (self.x0 < rect.x1)
            & (rect.x0 < self.x1)
            & (self.y0 < rect.y1)
            & (rect.y0 < self.y1)
        )

    def covered(self, seq: int, rect: Any) -> bool:
        """Whether a box drawn after `seq` contains `rect`"""
        return bool(((self.seq > seq) & self.contain(rect)).any())

    def last_under(self, seq: int, rect: Any) -> int:
        """Log index of the last box drawn before `seq` that intersects `rect`, else -1"""
        found = (self.seq < seq) & self.touch(rect)
        return int(self.seq[found].max()) if found.any() else -1


def scan_page(
    page: Any, number: int, limits: Thresholds, marks: Marks | None = None
) -> Iterator[HiddenSpan]:
    """Hidden spans of one page, reusing `marks` when the caller already collected them"""
    import numpy as np
    import pymupdf

    marks = marks or Marks(page)
    spans = [(s, "".join(chr(ch[0]) for ch in s["chars"]).strip()) for s in marks.trace]
    spans = [(s, text) for s, text in spans if text]
    if not spans:
        return
    # The raw tuples, since get_drawings wraps every path and point of a vector-heavy page
    # in Rect and Point objects
    drawn = [d for d in page.get_cdrawings() if d.get("fill")]
    fills = Boxes([(d["seqno"], *d["rect"]) for d in drawn])
    colors = [d["fill"] for d in drawn]
    for s, text in spans:
        rect, seq = pymupdf.Rect(s["bbox"]), s["seqno"]
        behind = np.flatnonzero((fills.seq < seq) & fills.contain(rect))
        bg = luminance(colors[behind[-1]]) if len(behind) else 1.0
        image = marks.images.last_under(seq, rect)
        # The real background there is pixels, so the contrast render covers it instead
        over_image = image >= 0 and (not len(behind) or image > fills.seq[behind[-1]])
        fg = luminance(s["color"])
        reasons = []
        if s["type"] == 3:
            reasons.append("render mode 3")
        if s["opacity"] < limits.opacity:
            reasons.append(f"opacity {s['opacity']:.2f}")
        if s["size"] < limits.size:
            reasons.append(f"size {s['size']:.1f}pt")
        if not rect.intersects(page.rect):
            reasons.append("off page")
        if s.get("layer"):
            reasons.append(f"layer {s['layer']}")
        if not over_image and abs(fg - bg) < limits.contrast:
            reasons.append(f"color matches background by {abs(fg - bg):.2f}")
        if marks.covers.covered(seq, rect):
            reasons.append("covered by a later fill")
        if reasons:
            yield HiddenSpan(
                page=number,
                text=text,
                reasons=tuple(reasons),
                bbox=tuple(round(v, 1) for v in rect),
                color=round(fg, 2),
                background=round(bg, 2),
                size=round(s["size"], 1),
                opacity=round(s["opacity"], 2),
            )


DEFAULT_LIMITS = Thresholds()
