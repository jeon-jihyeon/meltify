"""Which values the engines agree on and which they dispute

A value counts as agreed only when two or more engines read it the same number of times
and at least one of them runs locally. Two LLMs agreeing on a guess is still disputed
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

from meltify.engines.ocr import LOCAL, TextBox

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
    counts = {
        r.name: Counter(v for b in r.boxes for v in _values(b.text, pattern)) for r in readings
    }
    kinds = {r.name: r.kind for r in readings}
    verdicts = []
    for value in sorted(set().union(*counts.values()) if counts else set()):
        by = {name: c[value] for name, c in counts.items()}
        agreed = len(by) >= 2 and len(set(by.values())) == 1 and LOCAL in kinds.values()
        bbox = next(
            (
                b.bbox
                for r in readings
                for b in r.boxes
                if b.bbox is not None and value in _values(b.text, pattern)
            ),
            None,
        )
        verdicts.append(Verdict(value, by, agreed, bbox))
    return verdicts
