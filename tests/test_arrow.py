import sys
from pathlib import Path

import pytest

from meltify.converters import arrow, pick
from meltify.evidence import Src
from meltify.safe import MissingTool

pa = pytest.importorskip("pyarrow")
ipc = pytest.importorskip("pyarrow.ipc")


def _table(rows: int = 30):
    table = pa.table(
        {
            "id": list(range(1, rows + 1)),
            "name": ["NULL" if i == 2 else f"item|{i}" for i in range(1, rows + 1)],
            "doc": pa.array([None] * rows, pa.json_()),
            "tags": [[i, i + 1] for i in range(rows)],
            "ok": [i % 2 == 0 for i in range(rows)],
        }
    )
    return table.replace_schema_metadata({"producer": '{"query": "select 1"}'})


def _file(path: Path, table, batch: int = 10, compression: str | None = None) -> Path:
    options = ipc.IpcWriteOptions(compression=compression)
    with ipc.new_file(path, table.schema, options=options) as w:
        w.write_table(table, max_chunksize=batch)
    return path


def _stream(path: Path, table, batch: int = 10) -> Path:
    with ipc.new_stream(path, table.schema) as w:
        w.write_table(table, max_chunksize=batch)
    return path


def melt(path: Path):
    kind, convert = pick(path)
    return kind, convert(path, Src(path.name))


def test_file_melts_metadata_summary_and_sample(tmp_path):
    kind, out = melt(_file(tmp_path / "t.arrow", _table()))
    assert kind == "arrow"
    meta, summary, sample = out.blocks
    assert [b.src.cite() for b in out.blocks] == ["t.arrow", "t.arrow", "t.arrow:1"]
    assert meta.text == 'producer: {"query": "select 1"}'
    assert summary.text.splitlines() == [
        "t.arrow: computed over all 30 rows",
        "| column | type | nulls | min | max | distinct |",
        "|---|---|---|---|---|---|",
        "| id | int64 | 0 | 1 | 30 | 30 |",
        '| name | string | 0 | "NULL" | item\\|9 | 30 |',
        "| doc | arrow.json | 30 |  |  | 0 |",
        "| tags | list<item: int64> | 0 |  |  |  |",
        "| ok | bool | 0 | false | true | 2 |",
    ]
    lines = sample.text.splitlines()
    assert lines[:4] == [
        "t.arrow: first 20 of 30 rows",
        "columns| id (int64) | name (string) | doc (arrow.json) | tags (list<item: int64>)"
        " | ok (bool)",
        "row=1| 1 | item\\|1 | NULL | [0, 1] | true",
        'row=2| 2 | "NULL" | NULL | [1, 2] | false',
    ]
    assert len(lines) == 22 and out.needs == []


def test_feather_and_stream_suffixes_and_magic_sniffs(tmp_path):
    feather = pytest.importorskip("pyarrow.feather")
    table = _table(3)
    feather.write_feather(table, tmp_path / "a.feather")
    _stream(tmp_path / "a.arrows", table)
    _file(tmp_path / "noext", table)
    _stream(tmp_path / "s.bin", table)
    for name in ("a.feather", "a.arrows", "noext", "s.bin"):
        kind, out = melt(tmp_path / name)
        assert kind == "arrow", name
        assert out.blocks[-1].text.startswith(f"{name}: 3 rows"), name
    # The continuation marker alone, without a padded length, isn't a stream
    (tmp_path / "junk").write_bytes(b"\xff\xff\xff\xff\x03\x00\x00\x00rest")
    assert pick(tmp_path / "junk")[0] != "arrow"


def test_a_stream_cut_mid_batch_keeps_the_whole_batches(tmp_path):
    path = _stream(tmp_path / "t.arrows", _table())
    data = path.read_bytes()
    path.write_bytes(data[: len(data) - 200])
    _, out = melt(path)
    assert out.blocks[1].text.splitlines()[0] == "t.arrows: computed over all 20 rows"
    assert len(out.needs) == 1 and out.needs[0].startswith("stream ended early after 2 batches")


def test_the_cut_need_counts_its_batches_in_words(tmp_path):
    path = _stream(tmp_path / "t.arrows", _table(), batch=30)
    data = path.read_bytes()
    path.write_bytes(data[: len(data) - 20])
    _, out = melt(path)
    assert out.needs[0].startswith("stream ended before its first whole batch: ")
    path = _stream(tmp_path / "t.arrows", _table(), batch=15)
    data = path.read_bytes()
    path.write_bytes(data[: len(data) - 20])
    _, out = melt(path)
    assert out.needs[0].startswith("stream ended early after 1 batch: ")


def test_a_file_without_its_footer_is_read_as_a_stream_and_says_so(tmp_path):
    path = _file(tmp_path / "t.arrow", _table())
    data = path.read_bytes()
    path.write_bytes(data[: len(data) - 40])
    _, out = melt(path)
    assert out.blocks[1].text.splitlines()[0] == "t.arrow: computed over all 30 rows"
    assert out.needs == ["file footer missing, read as a stream"]


def test_compressed_batches_stop_before_they_unpack_past_the_limit(tmp_path):
    table = pa.table({"x": pa.array([0] * 1_000_000, pa.int64())})
    path = _file(tmp_path / "z.arrow", table, batch=400_000, compression="zstd")
    # A few KB on disk, 8 MB once unpacked
    assert path.stat().st_size < 100_000
    loaded = arrow.load(path, 5 << 20)
    assert loaded.table.num_rows == 400_000 and loaded.unpacked == 3_200_000
    assert loaded.needs == ["batch 2 not read, it unpacks past 5 MiB"]
    full = arrow.load(path)
    assert full.table.num_rows == 1_000_000 and full.needs == []


def test_a_batch_that_fails_validation_still_counts_what_it_declares(tmp_path, monkeypatch):
    table = pa.table({"x": pa.array([0] * 1000, pa.int64())})
    path = _file(tmp_path / "z.arrow", table, batch=500, compression="zstd")
    opened = ipc.open_stream

    class Failing:
        def __init__(self, source):
            self.reader, self.read = opened(source), 0
            self.schema = self.reader.schema

        def read_next_batch(self):
            self.read += 1
            if self.read == 2:
                raise pa.ArrowInvalid("bad offsets")
            return self.reader.read_next_batch()

    monkeypatch.setattr(ipc, "open_stream", Failing)
    loaded = arrow.load(path)
    assert loaded.table.num_rows == 500 and loaded.unpacked == 4000
    # Claims spend the declared bytes, so the read must spend the same
    assert loaded.declared == arrow.unpacks(path) == 8000
    assert loaded.needs[0].startswith("stream ended early after 1 batch: ")


def test_uncompressed_batches_stay_in_the_map(tmp_path):
    loaded = arrow.load(_file(tmp_path / "t.arrow", _table()))
    assert loaded.unpacked == 0 and loaded.table.num_rows == 30


def test_feather_v1_and_garbage_say_what_they_are(tmp_path):
    (tmp_path / "old.feather").write_bytes(b"FEA1" + b"\0" * 60)
    with pytest.raises(ValueError, match="Feather v1 isn't read"):
        arrow.load(tmp_path / "old.feather")
    (tmp_path / "bad.arrows").write_bytes(b"\xff\xff\xff\xff\x08\x00\x00\x00" + b"\x01" * 16)
    with pytest.raises(ValueError, match="not an Arrow IPC file or stream"):
        arrow.load(tmp_path / "bad.arrows")


def test_missing_pyarrow_names_the_extra(tmp_path, monkeypatch):
    path = _file(tmp_path / "t.arrow", _table(1))
    monkeypatch.setitem(sys.modules, "pyarrow", None)
    with pytest.raises(MissingTool) as e:
        arrow.convert(path, Src(path.name))
    assert e.value.hint == "meltify doctor --install parquet"


def test_values_past_python_datetime_range_fall_back_per_cell(tmp_path):
    table = pa.table(
        {
            "t": pa.array([400_000_000_000, 1], pa.timestamp("s")),
            "d": pa.array([2**31 - 1, 0], pa.date32()),
        }
    )
    _, out = melt(_file(tmp_path / "t.arrow", table))
    summary, sample = out.blocks
    assert "| t | timestamp[s] | 0 | 1970-01-01 00:00:01 | 14645-06-30 15:06:40 | 2 |" in (
        summary.text
    )
    rows = sample.text.splitlines()[2:]
    # Only the out-of-range cell falls back, its neighbours stay Python values
    assert rows[0].startswith("row=1| 14645-06-30 15:06:40 | ")
    assert rows[1] == "row=2| 1970-01-01 00:00:01 | 1970-01-01"


def test_one_row_is_singular(tmp_path):
    _, out = melt(_file(tmp_path / "t.arrow", pa.table({"x": [1]})))
    assert out.blocks[0].text.splitlines()[0] == "t.arrow: computed over 1 row"
    assert out.blocks[1].text.splitlines()[0] == "t.arrow: 1 row"


def test_the_message_scan_closes_its_memory_map(tmp_path, monkeypatch):
    path = _file(tmp_path / "t.arrow", _table())
    opened = []
    memory_map = pa.memory_map

    def tracking(*args, **kwargs):
        opened.append(memory_map(*args, **kwargs))
        return opened[-1]

    monkeypatch.setattr(pa, "memory_map", tracking)
    arrow.unpacks(path)
    assert opened and all(m.closed for m in opened)


def test_load_closes_its_memory_map_on_every_path(tmp_path, monkeypatch):
    path = _file(tmp_path / "t.arrow", _table())
    (tmp_path / "bad.arrows").write_bytes(b"\xff\xff\xff\xff\x08\x00\x00\x00" + b"\x01" * 16)
    opened = []
    memory_map = pa.memory_map

    def tracking(*args, **kwargs):
        opened.append(memory_map(*args, **kwargs))
        return opened[-1]

    monkeypatch.setattr(pa, "memory_map", tracking)
    loaded = arrow.load(path)
    assert opened and all(m.closed for m in opened)
    # The buffers keep the mapping after its file handle is closed
    assert pa.compute.sum(loaded.table["id"]).as_py() == 465
    opened.clear()
    with pytest.raises(ValueError, match="not an Arrow IPC file or stream"):
        arrow.load(tmp_path / "bad.arrows")
    assert opened and all(m.closed for m in opened)


def test_no_rows_reads_as_zero(tmp_path):
    _, out = melt(_file(tmp_path / "t.arrow", pa.table({"x": pa.array([], pa.int64())})))
    assert out.blocks[0].text.splitlines()[0] == "t.arrow: computed over 0 rows"
    assert out.blocks[1].text.splitlines()[0] == "t.arrow: 0 rows"


def test_a_distinct_count_past_the_memory_cap_is_not_computed(tmp_path, monkeypatch):
    path = _file(tmp_path / "t.arrow", pa.table({"x": list(range(30))}))
    # 30 rows estimate 30 * 512 plus 4 per column byte, so 15,000 bytes is just under
    monkeypatch.setattr(arrow, "MAX_DISTINCT_BYTES", 15_000)
    _, out = melt(path)
    assert "| x | int64 | 0 | 0 | 29 | not computed |" in out.blocks[0].text
    monkeypatch.setattr(arrow, "MAX_DISTINCT_BYTES", 30 * 512 + 30 * 8 * 4)
    _, out = melt(path)
    assert "| x | int64 | 0 | 0 | 29 | 30 |" in out.blocks[0].text
