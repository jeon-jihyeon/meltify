"""Which values the engines agree on and which they dispute

A value counts as agreed only when every engine reads it the same number of times and at
least one engine runs locally. Two LLMs agreeing on a guess is still disputed
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Any

from meltify.engines.ocr import LOCAL, TextBox
from meltify.evidence import Src, finding

NUMBERS = re.compile(r"\d+(?:[.,]\d+)*")
TOKENS = re.compile(r"\w+(?:[.,]\w+)*")


@dataclass(frozen=True)
class EngineReading:
    name: str
    kind: str
    boxes: list[TextBox]


@dataclass(frozen=True)
class Verdict:
    value: str
    counts: dict[str, int]
    agreed: bool
    bbox: tuple[float, float, float, float] | None


def _values(text: str, pattern: re.Pattern[str]) -> list[str]:
    # NFKC folds full-width digits, so １２ and 12 compare equal
    return pattern.findall(unicodedata.normalize("NFKC", text))


def compare(readings: list[EngineReading], mode: str = "numbers") -> list[Verdict]:
    pattern = NUMBERS if mode == "numbers" else TOKENS
    read = [(r, [(b.bbox, _values(b.text, pattern)) for b in r.boxes]) for r in readings]
    counts = {r.name: Counter(v for _, values in boxes for v in values) for r, boxes in read}
    # Each value cites the first box, in engine order, that has a bbox and reads it
    first: dict[str, tuple[float, float, float, float]] = {}
    for _, boxes in read:
        for bbox, values in boxes:
            if bbox is not None:
                for v in values:
                    first.setdefault(v, bbox)
    kinds = {r.name: r.kind for r in readings}
    verdicts = []
    for value in sorted(set().union(*counts.values()) if counts else set()):
        by = {name: c[value] for name, c in counts.items()}
        agreed = len(by) >= 2 and len(set(by.values())) == 1 and LOCAL in kinds.values()
        verdicts.append(Verdict(value, by, agreed, first.get(value)))
    return verdicts


def tally(v: Verdict) -> str:
    """How often each engine read the value, like `vision 1, paddle 0`"""
    return ", ".join(f"{name} {n}" for name, n in v.counts.items())


def disputed_row(src: Src, v: Verdict, **extra: Any) -> dict[str, Any]:
    """The result row for a value the engines dispute, cited at `src`"""
    return finding(
        src,
        **extra,
        type="disputed",
        value=v.value,
        counts=v.counts,
        text=f"{v.value}  ({tally(v)})",
    )
