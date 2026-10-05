"""OCR engines behind one interface

Local engines run on this machine and cost nothing.
LLM engines send the image to a provider and run only when you name them
"""

from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from meltify import lang as langs
from meltify import llm

LOCAL = "local"
REMOTE = "llm"


@dataclass(frozen=True)
class TextBox:
    text: str
    bbox: tuple[float, float, float, float] | None = None  # px in the image the engine saw
    conf: float | None = None


class Engine(Protocol):
    name: str
    kind: str

    def missing(self) -> str | None: ...

    def recognize(self, image: Path, size: tuple[int, int]) -> list[TextBox]: ...


@dataclass
class Vision:
    lang: str
    name: str = "vision"
    kind: str = LOCAL

    def missing(self) -> str | None:
        if sys.platform != "darwin":
            return "Apple Vision needs macOS"
        return None if importlib.util.find_spec("ocrmac") else "pip install ocrmac"

    def recognize(self, image: Path, size: tuple[int, int]) -> list[TextBox]:
        from ocrmac import ocrmac

        w, h = size
        found = ocrmac.OCR(
            str(image), recognition_level="accurate", language_preference=langs.vision(self.lang)
        ).recognize()
        boxes = []
        for text, conf, (x, y, bw, bh) in found:
            # Vision boxes are normalized, with the origin at the bottom left
            bbox = (x * w, (1 - y - bh) * h, (x + bw) * w, (1 - y) * h)
            boxes.append(TextBox(text, bbox, round(float(conf), 3)))
        return boxes


@dataclass
class Paddle:
    lang: str
    name: str = "paddle"
    kind: str = LOCAL
    _ocr: Any = field(default=None, init=False, repr=False)

    def missing(self) -> str | None:
        return (
            None if importlib.util.find_spec("paddleocr") else "meltify doctor --install ocr-paddle"
        )

    def recognize(self, image: Path, size: tuple[int, int]) -> list[TextBox]:
        # Model loading takes seconds, so reuse one instance for every page and tile
        if self._ocr is None:
            # The host check adds seconds and a warning to every run, and the default host works
            os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
            from paddleocr import PaddleOCR

            models: dict[str, Any] = {"lang": langs.paddle(self.lang)}
            resolve = getattr(PaddleOCR, "_get_ocr_model_names", None)
            if resolve is not None:
                # PP-OCRv5 pairs every language with the server detector, which takes
                # about 100 s per A4 page on CPU. The mobile one takes about 6 s
                # and read the same text on a scanned Korean page
                _, rec = resolve(None, models["lang"], None)
                if rec:
                    models = {
                        "text_detection_model_name": "PP-OCRv5_mobile_det",
                        "text_recognition_model_name": rec,
                    }
            self._ocr = PaddleOCR(
                **models,
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
            )
        res = self._ocr.predict(str(image))[0]
        boxes = res.get("rec_boxes")
        scores = res.get("rec_scores")
        return [
            TextBox(
                t,
                tuple(float(v) for v in boxes[i]) if boxes is not None else None,
                round(float(scores[i]), 3) if scores is not None else None,
            )
            for i, t in enumerate(res["rec_texts"])
        ]


@dataclass
class Remote:
    """Vision LLM that returns plain lines without positions"""

    name: str
    model: str
    key_env: str
    base_url: str = ""
    kind: str = REMOTE

    def missing(self) -> str | None:
        return None if os.environ.get(self.key_env) else f"export {self.key_env}=..."

    def recognize(self, image: Path, size: tuple[int, int]) -> list[TextBox]:
        api_key = llm.key(self.key_env)
        if self.name == "claude":
            text = llm.anthropic_vision(image, self.model, api_key)
        elif self.name == "gemini":
            text = llm.gemini_vision(image, self.model, api_key)
        else:
            text = llm.openai_vision(image, self.model, api_key, self.base_url)
        return [TextBox(line.strip()) for line in text.splitlines() if line.strip()]


@dataclass
class Reading:
    """Lines someone else already read, like an agent looking at the image itself"""

    name: str
    lines: list[str]
    kind: str = REMOTE

    def missing(self) -> str | None:
        return None

    def recognize(self, image: Path, size: tuple[int, int]) -> list[TextBox]:
        return [TextBox(line) for line in self.lines]


def build(name: str, settings: dict[str, Any]) -> Engine:
    lang = settings.get("lang", "ko")
    conf = settings.get("llm", {})
    if name == "vision":
        return Vision(lang)
    if name == "paddle":
        return Paddle(lang)
    if name == "claude":
        return Remote("claude", conf["claude_model"], conf["anthropic_key_env"])
    if name == "gemini":
        return Remote("gemini", conf["gemini_model"], conf["gemini_key_env"])
    if name == "openai":
        return Remote(
            "openai", conf["openai_model"], conf["openai_key_env"], conf["openai_base_url"]
        )
    raise ValueError(f"unknown OCR engine {name}, use vision, paddle, claude, gemini or openai")


def select(spec: str, settings: dict[str, Any]) -> list[Engine]:
    """auto keeps every installed local engine and never pays for an LLM"""
    if spec == "auto":
        engines = [build(n, settings) for n in ("vision", "paddle")]
        return [e for e in engines if e.missing() is None]
    return [build(n.strip(), settings) for n in spec.split(",") if n.strip()]
