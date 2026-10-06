"""Parquet tables: the schema, a per-column profile, then the first rows cited by row number

A cite like `a.parquet:7` points at the seventh data row, counted from 1
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from meltify.converters import Block, Converted, run
from meltify.converters.limits import MAX_MEMBER_BYTES, human_bytes
from meltify.converters.tables import sample_cell
from meltify.evidence import Src
from meltify.needs import not_read
from meltify.safe import MissingTool


def _groups(meta, limit: int) -> tuple[list[int], str | None]:
    """Leading row groups that hold the first `limit` rows, and why they stop short if they do

    Arrow unpacks a whole page at a time, and a few KB of zstd can declare one value of
    hundreds of MB, so the sizes the footer declares are checked before anything is read
    """
    groups: list[int] = []
    rows = size = 0
    for i in range(meta.num_row_groups):
        if rows >= limit:
            break
        group = meta.row_group(i)
        columns = sum(group.column(j).total_uncompressed_size for j in range(group.num_columns))
        size += max(group.total_byte_size, columns)
        if size > MAX_MEMBER_BYTES:
            past = human_bytes(MAX_MEMBER_BYTES)
            return groups, f"row group {i + 1} not read, it unpacks past {past}"
        groups.append(i)
        rows += group.num_rows
    return groups, None


def _head(f, limit: int, groups: list[int]) -> list[tuple]:
    rows: list[tuple] = []
    # Batches end at row group boundaries, so one batch may hold fewer rows than asked
    for batch in f.iter_batches(batch_size=limit, row_groups=groups):
        # Tuples, not dicts, so two columns with the same name both survive
        rows += list(zip(*(c.to_pylist() for c in batch.columns), strict=True))
        if len(rows) >= limit:
            break
    return rows[:limit]


def _profile(meta) -> str:
    """Min, max and nulls per column from the footer statistics, without reading any data

    A row group that wrote no statistics leaves that column's cells blank, since the rest
    can't speak for it
    """
    lines = ["| column | min | max | nulls |", "|---|---|---|---|"]
    for j in range(meta.num_columns):
        stats = [meta.row_group(i).column(j).statistics for i in range(meta.num_row_groups)]
        low = high = nulls = ""
        if stats and all(s is not None and s.has_min_max for s in stats):
            low, high = min(s.min for s in stats), max(s.max for s in stats)
        if stats and all(s is not None and s.has_null_count for s in stats):
            nulls = f"{sum(s.null_count for s in stats):,}"
        name = meta.schema.column(j).path
        cells = " | ".join(map(sample_cell, (name, low, high)))
        lines.append(f"| {cells} | {nulls} |")
    return "\n".join(lines)


def convert(path: Path, src: Src) -> Converted:
    try:
        import pyarrow.parquet as pq
    except ImportError as e:
        raise MissingTool("pyarrow", "meltify doctor --install parquet") from e

    out = Converted("parquet")
    with pq.ParquetFile(path) as f:
        total = f.metadata.num_rows
        fields = list(f.schema_arrow)
        schema = "\n".join(f"{field.name}: {field.type}" for field in fields)
        out.blocks.append(Block(src, f"{total:,} rows\n{schema}"))
        if f.metadata.num_row_groups:
            out.blocks.append(Block(src, _profile(f.metadata)))
        limit = run.current().parquet_rows or total
        groups, cut = _groups(f.metadata, limit)
        rows = _head(f, limit, groups) if groups else []

    if rows:
        lines = [
            "| row | " + " | ".join(sample_cell(field.name) for field in fields) + " |",
            "|---" * (len(fields) + 1) + "|",
        ]
        for n, row in enumerate(rows, start=1):
            lines.append(f"| {n} | " + " | ".join(sample_cell(v) for v in row) + " |")
        out.blocks.append(Block(replace(src, line=1), "\n".join(lines)))
    if (rest := total - len(rows)) > 0:
        shown = f"first {len(rows):,} shown, read.parquet_rows sets how many"
        out.needs.append(not_read(rest, "more row", shown))
    if cut:
        out.needs.append(cut)
    return out
