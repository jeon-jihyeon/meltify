"""Turn one input file into cited text blocks

Every converter returns blocks whose Src points back into the original file,
so you can quote the rendered markdown with a citation for each block
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from meltify.evidence import Src

TEXT = {
    ".txt",
    ".md",
    ".csv",
    ".tsv",
    ".json",
    ".jsonl",
    ".log",
    ".xml",
    ".yaml",
    ".yml",
    ".srt",
    ".vtt",
}
IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic"}
MEDIA = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".mp4", ".mov", ".mkv", ".webm", ".avi"}
OFFICE = {".docx", ".pptx", ".msg", ".html", ".htm", ".xls", ".epub"}


@dataclass
class Block:
    src: Src
    text: str


@dataclass
class Converted:
    kind: str
    blocks: list[Block] = field(default_factory=list)
    needs: list[str] = field(default_factory=list)
    attachments: list[tuple[str, bytes]] = field(default_factory=list)
    hidden: int = 0

    @property
    def chars(self) -> int:
        return sum(len(b.text) for b in self.blocks)

    def markdown(self, origin: str) -> str:
        parts = [f"<!-- meltify source: {origin} -->"]
        for b in self.blocks:
            parts.append(f"## {b.src.cite()}\n{b.text.rstrip()}\n")
        if self.needs:
            parts.append(f"<!-- meltify needs: {', '.join(self.needs)} -->")
        return "\n".join(parts) + "\n"


Converter = Callable[[Path, Src], Converted]


def pick(path: Path) -> tuple[str, Converter]:
    from meltify.converters import mail, office, pdf, sheet, text

    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return "pdf", pdf.convert
    if suffix in {".xlsx", ".xlsm"}:
        return "sheet", sheet.convert
    if suffix == ".eml":
        return "mail", mail.convert
    if suffix in TEXT:
        return "text", text.convert
    if suffix in IMAGES:
        return "image", lambda p, s: Converted("image", needs=["ocr"])
    if suffix in MEDIA:
        return "media", lambda p, s: Converted("media", needs=["media"])
    if suffix in OFFICE:
        return "office", office.convert
    return "unknown", text.convert_unknown
