"""OCR engines behind one interface

Local engines run on this machine and cost nothing.
LLM engines send the image to a provider and run only when you name them, unless it's an
endpoint you serve yourself, which auto runs too
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
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
class Remote:
    """Vision LLM that returns plain lines without positions"""

    name: str
    model: str
    key_env: str
    base_url: str = ""
    # Set for a model you serve yourself, which auto runs since it costs nothing. It's still a
    # vision LLM that can guess, so its readings need a local engine to count as agreed
    endpoint: llm.Endpoint | None = None
    kind: str = REMOTE

    def missing(self) -> str | None:
        if self.endpoint is not None:
            return self.endpoint.missing()
        return None if os.environ.get(self.key_env) else f"export {self.key_env}=..."

    def recognize(self, image: Path, size: tuple[int, int]) -> list[TextBox]:
        if (ep := self.endpoint) is not None:
            text = llm.openai_vision(
                image, ep.model, ep.api_key, ep.base_url, ep.prompt, ep.timeout
            )
        elif self.name == "claude":
            text = llm.anthropic_vision(image, self.model, llm.key(self.key_env))
        elif self.name == "gemini":
            text = llm.gemini_vision(image, self.model, llm.key(self.key_env))
        else:
            text = llm.openai_vision(image, self.model, llm.key(self.key_env), self.base_url)
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


# Every built-in OCR engine by name, made from the OCR language and the [llm] settings.
# Every one is safe to call from several threads at once, since each builds its own request
ENGINES: dict[str, Callable[[str, Mapping[str, Any]], Engine]] = {
    "vision": lambda lang, conf: Vision(lang),
    "claude": lambda lang, conf: Remote("claude", conf["claude_model"], conf["anthropic_key_env"]),
    "gemini": lambda lang, conf: Remote("gemini", conf["gemini_model"], conf["gemini_key_env"]),
    "openai": lambda lang, conf: Remote(
        "openai", conf["openai_model"], conf["openai_key_env"], conf["openai_base_url"]
    ),
}
# Body text comes from the first of these that read anything, and auto picks every one installed
LOCAL_ENGINES = ("vision",)
# What to set up when no local engine is there
INSTALL_HINT = (
    "pip install ocrmac on macOS, or serve an OCR model behind an OpenAI-compatible endpoint "
    "and add it under [ocr.endpoints.NAME]"
)


def endpoints(settings: Mapping[str, Any]) -> dict[str, llm.Endpoint]:
    """The OCR models you serve yourself, by the name you gave each"""
    return llm.endpoints("ocr", settings["ocr"]["endpoints"], set(ENGINES), llm.TIMEOUT)


def names(settings: Mapping[str, Any]) -> str:
    """Every engine name for help and errors, like `vision, claude, gemini or openai`"""
    *rest, last = [*ENGINES, *endpoints(settings)]
    return f"{', '.join(rest)} or {last}"


def build(name: str, settings: Mapping[str, Any]) -> Engine:
    make = ENGINES.get(name)
    if make is not None:
        return make(settings["lang"], settings["llm"])
    ep = endpoints(settings).get(name)
    if ep is None:
        raise ValueError(f"unknown OCR engine {name}, use {names(settings)}")
    return Remote(name, ep.model, ep.key_env, ep.base_url, ep)


def select(spec: str, settings: Mapping[str, Any]) -> list[Engine]:
    """auto keeps every installed local engine and every endpoint, and never pays for an LLM"""
    if spec == "auto":
        engines = [build(n, settings) for n in (*LOCAL_ENGINES, *endpoints(settings))]
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
