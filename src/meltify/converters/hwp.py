from __future__ import annotations

import warnings
from dataclasses import replace
from pathlib import Path
from typing import Any

from meltify.converters import Block, Converted, RecognizeJob
from meltify.converters.text import BLOCK_LINES
from meltify.evidence import Src

# Hwp5Error codes for a body that can't be read without a password or certificate
LOCKED = {"hwp5-password", "hwp5-distribution", "hwp5-drm"}
PICTURES = {"jpg", "jpeg", "png", "bmp", "gif", "tif", "tiff", "webp"}


def _cell(text: str) -> str:
    return text.strip().replace("|", "\\|").replace("\n", " ")


def table_markdown(table: Any) -> str | None:
    # A merged cell shows its text once at its anchor and stays blank where it spans
    rows = [[""] * table.column_count for _ in range(table.row_count)]
    for pos in table.iter_grid():
        if (pos.row, pos.column) == tuple(pos.anchor):
            rows[pos.row][pos.column] = _cell(pos.cell.text or "")
    if not any(any(r) for r in rows):
        return None
    lines = ["| " + " | ".join(rows[0]) + " |", "|---" * table.column_count + "|"]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines)


def _section(section: Any, src: Src, out: Converted) -> None:
    # A cite line is the paragraph's index in its section, so empty paragraphs still count
    lines: list[tuple[int, str]] = []
    width = len(str(len(section.paragraphs)))

    def flush() -> None:
        for start in range(0, len(lines), BLOCK_LINES):
            chunk = lines[start : start + BLOCK_LINES]
            text = "\n".join(f"{n:>{width}}| {t}" for n, t in chunk)
            out.blocks.append(Block(replace(src, line=chunk[0][0]), text))
        lines.clear()

    for n, para in enumerate(section.paragraphs, start=1):
        if text := para.text.strip():
            lines.append((n, text))
        tables = [md for t in para.tables if t.row_count and (md := table_markdown(t))]
        if tables:
            flush()
            out.blocks += [Block(replace(src, line=n), md) for md in tables]
    flush()


def _pictures(doc: Any, src: Src, out: Converted) -> None:
    items = {i.item_id: i for i in doc.media.images}
    seen: set[str] = set()
    counts: dict[int, int] = {}
    for ref in doc.media.picture_references():
        item = items.get(ref.binary_item_id_ref)
        # The same picture drawn twice would only OCR into a duplicate block
        if item is None or item.item_id in seen or item.format.lower() not in PICTURES:
            continue
        seen.add(item.item_id)
        section = ref.section_index + 1
        counts[section] = counts.get(section, 0) + 1
        data = doc.package.get_part(item.href)
        job_src = replace(src, section=section, img=counts[section])
        out.jobs.append(RecognizeJob("image", job_src, data=data))


def convert(path: Path, src: Src) -> Converted:
    from hwpx import HwpxDocument
    from hwpx.hwp5.errors import Hwp5Error

    out = Converted("hwp")
    try:
        # Conversion gaps are reported through conversion_report, not as warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            doc = HwpxDocument.open(path)
    except Hwp5Error as e:
        if e.code in LOCKED:
            out.needs.append("hwp encrypted")
            return out
        raise
    try:
        for i, section in enumerate(doc.sections, start=1):
            _section(section, replace(src, section=i), out)
        _pictures(doc, src, out)
        report = doc.conversion_report
        if report and report.unconverted:
            kinds = ", ".join(f"{k} x{n}" for k, n in sorted(report.unconverted.items()))
            out.needs.append(f"hwp skipped {kinds}")
    finally:
        doc.close()
    return out
