from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

from meltify.commands.media import parse_subtitles
from meltify.converters import Block, Converted
from meltify.converters.text import decode
from meltify.evidence import Src, _clock

# Cues per block, so a two-hour film doesn't become one giant quote
BLOCK_CUES = 50

ASS_TIME = re.compile(r"(\d+):(\d{2}):(\d{2})[.:](\d{1,3})")
ASS_FIELDS = ["layer", "start", "end", "style", "name", "marginl", "marginr", "marginv"]
ASS_FIELDS += ["effect", "text"]


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
        body = "\n".join(f"{_clock(s)}-{_clock(e)}| {said}" for s, e, said in chunk)
        out.blocks.append(Block(replace(src, t=(chunk[0][0], chunk[0][1])), body))
    return out
