from __future__ import annotations

import importlib.util
from pathlib import Path

from meltify.converters import Block, Converted
from meltify.evidence import Src
from meltify.safe import MissingTool


def convert(path: Path, src: Src) -> Converted:
    if importlib.util.find_spec("markitdown") is None:
        raise MissingTool("markitdown", "meltify doctor --install office")
    from markitdown import MarkItDown

    text = MarkItDown().convert(str(path)).markdown
    return Converted("office", [Block(src, text)] if text.strip() else [])
