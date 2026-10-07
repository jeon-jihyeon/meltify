from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from xml.parsers import expat

from meltify.converters import Block, Converted
from meltify.converters.embeds import gunzip
from meltify.converters.xmlsafe import REFUSED, refuse_entities
from meltify.evidence import Src

# Elements whose character data is what a viewer reads or a screen reader announces
TEXTUAL = {"text", "title", "desc", "foreignObject"}
LABELS = {"title": "title: ", "desc": "desc: "}


@dataclass
class _Frame:
    gone: bool  # display none or zero opacity, which no descendant can undo
    visibility: str  # inherited, but a descendant can set it back to visible


@dataclass
class _Item:
    kind: str
    line: int
    id: str | None
    parts: list[str] = field(default_factory=list)
    hidden: bool = False

    @property
    def text(self) -> str:
        return re.sub(r"\s+", " ", "".join(self.parts)).strip()


def _props(attrs: dict[str, str]) -> dict[str, str]:
    props = {k.rsplit(" ", 1)[-1]: v.strip() for k, v in attrs.items()}
    for decl in props.pop("style", "").split(";"):
        key, sep, value = decl.partition(":")
        if sep:
            props[key.strip().lower()] = value.strip()
    return props


def _zero(value: str | None) -> bool:
    if value is None:
        return False
    try:
        return float(value.removesuffix("%")) == 0
    except ValueError:
        return False


def _parse(data: bytes) -> list[_Item]:
    refuse_entities(data)
    parser = expat.ParserCreate(namespace_separator=" ")
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    stack = [_Frame(False, "visible")]
    open_items: list[tuple[int, _Item]] = []  # the stack depth that started each item
    items: list[_Item] = []

    def refuse(*_: object) -> None:
        raise ValueError(REFUSED)

    def start(name: str, attrs: dict[str, str]) -> None:
        props = _props(attrs)
        parent = stack[-1]
        gone = parent.gone or props.get("display") == "none" or _zero(props.get("opacity"))
        frame = _Frame(gone, props.get("visibility", parent.visibility))
        stack.append(frame)
        local = name.rsplit(" ", 1)[-1]
        if local in TEXTUAL and not open_items:
            item = _Item(local, parser.CurrentLineNumber, props.get("id"))
            open_items.append((len(stack), item))
            items.append(item)

    def chars(data: str) -> None:
        if open_items:
            item = open_items[-1][1]
            item.parts.append(data)
            frame = stack[-1]
            if data.strip() and (frame.gone or frame.visibility in ("hidden", "collapse")):
                item.hidden = True

    def end(name: str) -> None:
        if open_items and open_items[-1][0] == len(stack):
            open_items.pop()
        stack.pop()

    parser.EntityDeclHandler = refuse
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = chars
    parser.Parse(data, True)
    return [i for i in items if i.text]


def convert(path: Path, src: Src) -> Converted:
    out = Converted("svg", hidden_checked=True)
    loose: list[tuple[int, str]] = []

    def flush() -> None:
        if loose:
            width = len(str(loose[-1][0]))
            text = "\n".join(f"{n:>{width}}| {t}" for n, t in loose)
            out.blocks.append(Block(replace(src, line=loose[0][0]), text))
            loose.clear()

    data = path.read_bytes()
    if data.startswith(b"\x1f\x8b"):
        # An .svgz is the same XML gzipped
        unpacked = gunzip(data)
        if unpacked is None:
            return Converted("svg", needs=["svgz not read (corrupt or over the unpack limit)"])
        data = unpacked
    for item in _parse(data):
        text = LABELS.get(item.kind, "") + item.text
        if item.hidden:
            out.hidden += 1
            text = f"[hidden] {text}"
        # An id survives edits that shift lines, so it makes the steadier cite
        if item.id:
            flush()
            out.blocks.append(Block(replace(src, anchor=item.id, line=item.line), text))
        else:
            loose.append((item.line, text))
    flush()
    if out.hidden:
        out.needs.append(f"hidden {out.hidden} texts")
    return out
