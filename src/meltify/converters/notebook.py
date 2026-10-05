from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import replace
from pathlib import Path

from meltify.converters import Block, Converted, RecognizeJob
from meltify.evidence import Src

IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")
# A printed DataFrame or log can run to megabytes, which no reader quotes in full
MAX_OUTPUT = 10_000
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
DATA_URI = re.compile(r"data:[\w/+.-]+;base64,[A-Za-z0-9+/=\s]+")


def _text(value: str | list[str] | None) -> str:
    return "".join(value) if isinstance(value, list) else (value or "")


def _clip(text: str) -> str:
    text = DATA_URI.sub("data:...", ANSI.sub("", text)).rstrip()
    if len(text) <= MAX_OUTPUT:
        return text
    return text[:MAX_OUTPUT] + f"\n... ({len(text) - MAX_OUTPUT:,} more chars)"


def _image(data: str | list[str]) -> bytes | None:
    try:
        return base64.b64decode(_text(data))
    except (binascii.Error, ValueError):
        return None


def _outputs(cell: dict) -> tuple[list[str], list[bytes]]:
    texts: list[str] = []
    images: list[bytes] = []
    for out in cell.get("outputs", []):
        kind = out.get("output_type")
        if kind == "stream":
            texts.append(_text(out.get("text")))
        elif kind == "error":
            trace = (
                "\n".join(out.get("traceback", [])) or f"{out.get('ename')}: {out.get('evalue')}"
            )
            texts.append(trace)
        elif kind in ("execute_result", "display_data", "pyout"):
            data = out.get("data", out)
            for mime in IMAGE_TYPES:
                if mime in data and (raw := _image(data[mime])):
                    images.append(raw)
            # Plain text under an image is only a `<Figure ...>` repr
            for mime in ("text/markdown", "text/plain", "text"):
                if mime in data and not (images and mime == "text/plain"):
                    texts.append(_text(data[mime]))
                    break
    return [t for t in (_clip(t) for t in texts) if t], images


def convert(path: Path, src: Src) -> Converted:
    nb = json.loads(path.read_bytes())
    cells = nb.get("cells")
    if cells is None:
        # nbformat 3 kept cells under worksheets and code under `input`
        cells = [c for ws in nb.get("worksheets", []) for c in ws.get("cells", [])]
    meta = nb.get("metadata", {})
    lang = meta.get("kernelspec", {}).get("language") or meta.get("language_info", {}).get("name")
    out = Converted("notebook")
    for i, cell in enumerate(cells, start=1):
        cell_src = replace(src, cell=str(i))
        source = _clip(_text(cell.get("source", cell.get("input"))))
        kind = cell.get("cell_type", "code")
        texts, images = _outputs(cell) if kind == "code" else ([], [])
        for att in cell.get("attachments", {}).values():
            images += [raw for m in IMAGE_TYPES if m in att and (raw := _image(att[m]))]
        parts = [f"```{lang or ''}\n{source}\n```" if kind == "code" and source else source]
        parts += [f"Output:\n{t}" for t in texts]
        body = "\n\n".join(p for p in parts if p)
        if body:
            out.blocks.append(Block(cell_src, body))
        for k, raw in enumerate(images, start=1):
            out.jobs.append(RecognizeJob("image", replace(cell_src, img=k), data=raw))
    return out
