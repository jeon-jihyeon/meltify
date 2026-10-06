import contextvars
import json
import sys
from pathlib import Path

import pytest

from meltify import converters
from meltify.cli import main
from meltify.converters import Entry, parquet
from meltify.converters.run import RunContext, use
from meltify.evidence import Src
from meltify.safe import MissingTool


def _table(tmp_path: Path, rows: int, group: int = 64) -> Path:
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    table = pa.table(
        {
            "id": list(range(1, rows + 1)),
            "name": [None if i == 2 else f"item|{i}" for i in range(1, rows + 1)],
            "blob": [b"\x00\x01" for _ in range(rows)],
        }
    )
    path = tmp_path / "items.parquet"
    # Small row groups, so the head spans several batches
    pq.write_table(table, path, row_group_size=group)
    return path


def _convert(path: Path, rows: int):
    """The convert read runs, in a context set up the way read sets one up for each run"""
    run = contextvars.copy_context()
    run.run(use, RunContext(parquet_rows=rows))
    return run.run(parquet.convert, path, Src(str(path)))


def test_schema_then_rows_cited_by_row_number(tmp_path):
    path = _table(tmp_path, 3)
    out = parquet.convert(path, Src(str(path)))
    schema, _, rows = out.blocks
    assert schema.src.cite() == str(path)
    assert schema.text == "3 rows\nid: int64\nname: string\nblob: binary"
    assert rows.src.cite() == f"{path}:1"
    assert rows.text.splitlines() == [
        "| row | id | name | blob |",
        "|---|---|---|---|",
        "| 1 | 1 | item\\|1 | <blob 2 bytes> |",
        "| 2 | 2 | NULL | <blob 2 bytes> |",
        "| 3 | 3 | item\\|3 | <blob 2 bytes> |",
    ]
    assert out.needs == []


def test_rows_past_the_cap_are_counted_as_needs(tmp_path):
    path = _table(tmp_path, 250, group=30)
    out = _convert(path, rows=100)
    lines = out.blocks[2].text.splitlines()
    assert len(lines) == 2 + 100 and lines[-1].startswith("| 100 | 100 |")
    assert out.needs == [
        "150 more rows not read (first 100 shown, read.parquet_rows sets how many)"
    ]


def test_zero_cap_reads_every_row(tmp_path):
    path = _table(tmp_path, 250, group=30)
    out = _convert(path, rows=0)
    assert len(out.blocks[2].text.splitlines()) == 2 + 250 and out.needs == []


def test_profile_comes_from_footer_statistics_across_row_groups(tmp_path):
    path = _table(tmp_path, 250, group=30)
    profile = parquet.convert(path, Src(str(path))).blocks[1]
    assert profile.src.cite() == str(path)
    assert profile.text.splitlines() == [
        "| column | min | max | nulls |",
        "|---|---|---|---|",
        "| id | 1 | 250 | 0 |",
        "| name | item\\|1 | item\\|99 | 1 |",
        "| blob | <blob 2 bytes> | <blob 2 bytes> | 0 |",
    ]


def test_profile_leaves_cells_blank_without_statistics(tmp_path):
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    path = tmp_path / "bare.parquet"
    pq.write_table(pa.table({"n": [3, 1, 2]}), path, write_statistics=False)
    profile = parquet.convert(path, Src(str(path))).blocks[1]
    assert profile.text.splitlines()[-1] == "| n |  |  |  |"


def test_missing_pyarrow_names_the_extra(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "pyarrow", None)
    monkeypatch.setitem(sys.modules, "pyarrow.parquet", None)
    path = tmp_path / "x.parquet"
    path.write_bytes(b"PAR1")
    with pytest.raises(MissingTool) as e:
        parquet.convert(path, Src(str(path)))
    assert e.value.name == "pyarrow" and e.value.hint == "meltify doctor --install parquet"


def test_read_reports_the_cap_on_the_row(tmp_path, monkeypatch, capsys):
    path = _table(tmp_path, 3)
    monkeypatch.setitem(
        converters.SUFFIXES, ".parquet", Entry("parquet", "meltify.converters.parquet:convert")
    )
    # The cap is a setting, read once per run
    (tmp_path / "meltify.toml").write_text("[read]\nparquet_rows = 2\n")
    monkeypatch.chdir(tmp_path)
    main(["read", str(path), "--json", "--limit", "0"])
    row = json.loads(capsys.readouterr().out)["results"][0]
    assert row["kind"] == "parquet"
    assert row["needs"] == ["1 more row not read (first 2 shown, read.parquet_rows sets how many)"]
    assert f"## {path}:1\n| row | id | name | blob |" in Path(row["out"]).read_text(
        encoding="utf-8"
    )


def test_row_groups_that_unpack_past_the_cap_are_not_read(tmp_path, monkeypatch):
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    # A few KB of zstd can hold one value of hundreds of MB, which Arrow unpacks whole
    monkeypatch.setattr(parquet, "MAX_MEMBER_BYTES", 2000)
    path = tmp_path / "bomb.parquet"
    pq.write_table(
        pa.table({"s": ["small", "x" * 50_000]}), path, row_group_size=1, compression="zstd"
    )
    assert path.stat().st_size < 2000
    out = _convert(path, rows=200)
    assert out.blocks[2].text.splitlines()[2:] == ["| 1 | small |"]
    assert out.needs == [
        "1 more row not read (first 1 shown, read.parquet_rows sets how many)",
        "row group 2 not read, it unpacks past 2000 bytes",
    ]
