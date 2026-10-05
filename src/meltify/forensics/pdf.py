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


def scan_page(page: Any, number: int, limits: Thresholds) -> Iterator[HiddenSpan]:
    import pymupdf

    log = page.get_bboxlog()
    fills = [(d["seqno"], d["rect"], d["fill"]) for d in page.get_drawings() if d.get("fill")]
    for s in page.get_texttrace():
        text = "".join(chr(ch[0]) for ch in s["chars"]).strip()
        if not text:
            continue
        rect, seq = pymupdf.Rect(s["bbox"]), s["seqno"]
        behind = [(q, fill) for q, r, fill in fills if q < seq and r.contains(rect)]
        bg = luminance(behind[-1][1]) if behind else 1.0
        images = [
            q
            for q, (kind, box, *_) in enumerate(log[:seq])
            if kind == "fill-image" and pymupdf.Rect(box).intersects(rect)
        ]
        # The real background there is pixels, so the contrast render covers it instead
        over_image = bool(images) and (not behind or images[-1] > behind[-1][0])
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
        if any(
            kind in COVERING and pymupdf.Rect(box).contains(rect)
            for kind, box, *_ in log[seq + 1 :]
        ):
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


def scan(
    path: str, pages: list[int] | None = None, limits: Thresholds = DEFAULT_LIMITS
) -> list[HiddenSpan]:
    import pymupdf

    with pymupdf.open(path) as doc:
        numbers = list(range(1, doc.page_count + 1)) if pages is None else pages
        if not numbers:
            raise ValueError(f"no pages selected in {path}")
        return [span for n in numbers for span in scan_page(doc[n - 1], n, limits)]
