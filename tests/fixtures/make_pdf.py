"""PDFs that hide text in the ways read --hidden must catch"""

from __future__ import annotations

import io
from pathlib import Path

VISIBLE = "Visible normal text"
HIDDEN = {
    "WHITE SMALL": "size",
    "NEAR BG": "color matches background",
    "UNDER LAYER": "covered by a later fill",
    "INVISIBLE MODE": "render mode 3",
    "OFF PAGE": "off page",
}


def hidden_pdf(path: Path) -> Path:
    import pymupdf

    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 72), VISIBLE, fontsize=12)
    p.insert_text((72, 100), "WHITE SMALL", fontsize=3, color=(1, 1, 1))
    p.draw_rect(pymupdf.Rect(60, 120, 400, 160), color=None, fill=(0.9, 0.9, 0.95))
    p.insert_text((72, 145), "NEAR BG", fontsize=12, color=(0.88, 0.88, 0.93))
    p.insert_text((72, 200), "UNDER LAYER", fontsize=12)
    p.draw_rect(pymupdf.Rect(60, 185, 400, 210), color=None, fill=(0.2, 0.4, 0.6))
    p.insert_text((72, 240), "INVISIBLE MODE", fontsize=12, render_mode=3)
    p.insert_text((72, 900), "OFF PAGE", fontsize=12)
    doc.save(path)
    return path


def image_text_pdf(path: Path) -> Path:
    """A page whose only hidden text is pixels a shade off the background"""
    import pymupdf
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (600, 200), (240, 240, 240))
    ImageDraw.Draw(img).text((20, 80), "PIXEL SECRET", fill=(236, 236, 236))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_image(pymupdf.Rect(0, 0, 600, 200), stream=buf.getvalue())
    doc.save(path)
    return path


def white_on_image_pdf(path: Path) -> Path:
    """White text drawn over a dark image, which a reader sees clearly"""
    import pymupdf
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (400, 100), (20, 20, 20)).save(buf, format="PNG")
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_image(pymupdf.Rect(50, 50, 450, 150), stream=buf.getvalue())
    page.insert_text((72, 100), "WHITE ON DARK", fontsize=14, color=(1, 1, 1))
    doc.save(path)
    return path
