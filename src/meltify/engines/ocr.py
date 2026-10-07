"""OCR engines behind one interface

Local engines run on this machine and cost nothing.
LLM engines send the image to a provider and run only when you name them
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from meltify import lang as langs
from meltify import llm
from meltify.safe import MissingTool

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


@dataclass(frozen=True)
class Listing:
    """How to make one engine, and how a run may share it"""

    # From the OCR language and the [llm] settings
    make: Callable[[str, Mapping[str, Any]], Engine]
    kind: str
    # Safe to call from several threads at once. Vision and every remote engine build their
    # own request per call, while Paddle keeps one stateful model per instance
    shared: bool


# Every OCR engine by name. Local ones go first, since body text comes from the first local
# engine in this order that read anything, and auto picks every installed local one
ENGINES: dict[str, Listing] = {
    "vision": Listing(lambda lang, conf: Vision(lang), LOCAL, shared=True),
    "paddle": Listing(lambda lang, conf: Paddle(lang), LOCAL, shared=False),
    "claude": Listing(
        lambda lang, conf: Remote("claude", conf["claude_model"], conf["anthropic_key_env"]),
        REMOTE,
        shared=True,
    ),
    "gemini": Listing(
        lambda lang, conf: Remote("gemini", conf["gemini_model"], conf["gemini_key_env"]),
        REMOTE,
        shared=True,
    ),
    "openai": Listing(
        lambda lang, conf: Remote(
            "openai", conf["openai_model"], conf["openai_key_env"], conf["openai_base_url"]
        ),
        REMOTE,
        shared=True,
    ),
}
LOCAL_ENGINES = tuple(name for name, listing in ENGINES.items() if listing.kind == LOCAL)
SHARED = {name for name, listing in ENGINES.items() if listing.shared}
# What to install when no local engine is there
INSTALL_HINT = "pip install ocrmac on macOS, meltify doctor --install ocr-paddle elsewhere"


def names() -> str:
    """Every engine name for help and errors, like `vision, paddle, claude, gemini or openai`"""
    *rest, last = ENGINES
    return f"{', '.join(rest)} or {last}"


def build(name: str, settings: Mapping[str, Any]) -> Engine:
    listing = ENGINES.get(name)
    if listing is None:
        raise ValueError(f"unknown OCR engine {name}, use {names()}")
    return listing.make(settings["lang"], settings["llm"])


def select(spec: str, settings: Mapping[str, Any]) -> list[Engine]:
    """auto keeps every installed local engine and never pays for an LLM"""
    if spec == "auto":
        engines = [build(n, settings) for n in LOCAL_ENGINES]
        return [e for e in engines if e.missing() is None]
    return [build(n.strip(), settings) for n in spec.split(",") if n.strip()]


def ready(spec: str, settings: Mapping[str, Any]) -> list[Engine]:
    """The engines `spec` names, raising MissingTool for the first one that can't run"""
    chosen = select(spec, settings)
    for e in chosen:
        if (why := e.missing()) is not None:
            raise MissingTool(e.name, why)
    return chosen


def _overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def read_tiles(e: Engine, prepared: Any, tile_max: int, folder: Path) -> list[TextBox]:
    """Lines a remote engine reads tile by tile, each boxed by the tile it came from"""
    from meltify import imaging

    boxes = []
    seen: list[tuple[tuple[int, int, int, int], Counter[str]]] = []
    # Vision APIs shrink large images, so keep each tile under their limit
    for i, tile in enumerate(imaging.tiles(prepared, tile_max)):
        tile_path = imaging.save(tile.image, folder / f"tile{i}.png")
        rect = (tile.x, tile.y, tile.x + tile.image.width, tile.y + tile.image.height)
        lines = [b.text for b in e.recognize(tile_path, tile.image.size)]
        # Remote lines carry no position, so a line in the overlap comes back from both tiles.
        # Drop one copy for each line an overlapping tile already read
        before: Counter[str] = Counter()
        for r, c in seen:
            if _overlap(r, rect):
                before |= c
        seen.append((rect, Counter(lines)))
        for text in lines:
            if before[text]:
                before[text] -= 1
                continue
            boxes.append(TextBox(text, rect))
    return boxes
