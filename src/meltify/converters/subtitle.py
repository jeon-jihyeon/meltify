from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

from meltify.converters import Block, Converted
from meltify.converters.text import decode
from meltify.evidence import Src, clock

# Cues per block, so a two-hour film doesn't become one giant quote
BLOCK_CUES = 50

CUE = re.compile(r"(\d+:)?(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(\d+:)?(\d{2}):(\d{2})[.,](\d{3})")
ASS_TIME = re.compile(r"(\d+):(\d{2}):(\d{2})[.:](\d{1,3})")
ASS_FIELDS = ["layer", "start", "end", "style", "name", "marginl", "marginr", "marginv"]
ASS_FIELDS += ["effect", "text"]


def _seconds(h: str | None, m: str, s: str, ms: str) -> float:
    return int((h or "0:")[:-1]) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def parse_subtitles(text: str) -> list[tuple[float, float, str]]:
    """VTT or SRT cues with tags stripped and rolling repeats merged"""
    cues: list[tuple[float, float, str]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = CUE.search(lines[i])
        if not m:
            i += 1
            continue
        start = _seconds(m.group(1), m.group(2), m.group(3), m.group(4))
        end = _seconds(m.group(5), m.group(6), m.group(7), m.group(8))
        body = []
        i += 1
        while i < len(lines) and lines[i].strip():
            body.append(re.sub(r"<[^>]+>", "", lines[i]).strip())
            i += 1
        said = " ".join(b for b in body if b)
        # Auto captions repeat the previous line as they roll, so keep only the new text
        if cues and said.startswith(cues[-1][2]) and said != cues[-1][2]:
            said = said[len(cues[-1][2]) :].strip()
        if said and (not cues or said != cues[-1][2]):
            cues.append((round(start, 2), round(end, 2), said))
    return cues


def _ass_seconds(value: str) -> float | None:
    m = ASS_TIME.fullmatch(value.strip())
    if not m:
        return None
    h, mi, s, frac = m.groups()
    return int(h) * 3600 + int(mi) * 60 + int(s) + int(frac) / 10 ** len(frac)


def parse_ass(text: str) -> list[tuple[float, float, str]]:
    """Dialogue lines of an ASS or SSA script with override tags stripped"""
    fields = ASS_FIELDS
    cues = []
    for line in text.splitlines():
        key, _, value = line.partition(":")
        key = key.strip().lower()
        if key == "format":
            fields = [f.strip().lower() for f in value.split(",")]
        elif key == "dialogue" and "text" in fields:
            # Text is the last field and may itself contain commas
            parts = [p.strip() for p in value.split(",", len(fields) - 1)]
            row = dict(zip(fields, parts, strict=False))
            start = _ass_seconds(row.get("start", ""))
            end = _ass_seconds(row.get("end", ""))
            said = re.sub(r"\{[^}]*\}", "", row.get("text", ""))
            said = re.sub(r"\\[Nn]", " ", said).replace("\\h", " ")
            said = re.sub(r"\s+", " ", said).strip()
            if start is not None and end is not None and said:
                cues.append((start, end, said))
    return sorted(cues, key=lambda c: c[0])


def convert(path: Path, src: Src) -> Converted:
    text = decode(path.read_bytes())
    cues = parse_ass(text) if path.suffix.lower() == ".ass" else parse_subtitles(text)
    out = Converted("subtitle")
    if not cues:
        out.needs.append("no subtitle cues found")
        return out
    for i in range(0, len(cues), BLOCK_CUES):
        chunk = cues[i : i + BLOCK_CUES]
        body = "\n".join(f"{clock(s)}-{clock(e)}| {said}" for s, e, said in chunk)
        out.blocks.append(Block(replace(src, t=(chunk[0][0], chunk[0][1])), body))
    return out
