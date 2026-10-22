import json
import sys
from pathlib import Path

import pytest

from meltify.cli import main
from meltify.converters import datapackage, pick, unclaimed
from meltify.evidence import Src


def _package(folder: Path, resources: list, **extra) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "datapackage.json"
    path.write_text(json.dumps({"name": "pkg", **extra, "resources": resources}), "utf-8")
    return path


def melt(path: Path):
    kind, convert = pick(path)
    return kind, convert(path, Src(path.name))


def test_csv_table_with_missing_values_and_types(tmp_path):
    (tmp_path / "result.csv").write_text("id,name,note\n1,a,\\N\n2,NULL,x\n", "utf-8")
    schema = {
        "fields": [{"name": "id", "type": "integer"}, {"name": "name", "type": "string"}],
        "missingValues": ["\\N"],
    }
    path = _package(
        tmp_path,
        [{"name": "result", "path": "result.csv", "type": "table", "schema": schema}],
        created="2026-10-15T11:09:29+09:00",
        producer={"query": "select 1"},
    )
    kind, out = melt(path)
    assert kind == "datapackage"
    head, table = out.blocks
    assert head.src.cite() == "datapackage.json"
    assert head.text.splitlines()[:3] == [
        "name: pkg",
        "created: 2026-10-15T11:09:29+09:00",
        'producer: {"query": "select 1"}',
    ]
    assert "- result: path=result.csv, type=table" in head.text
    assert table.src.cite() == "datapackage.json#result!row=1"
    assert table.text.splitlines() == [
        "result: 2 rows",
        "columns| id (integer) | name (string) | note",
        "row=1| 1 | a | NULL",
        'row=2| 2 | "NULL" | x',
    ]
    assert out.needs == []


def test_an_objects_run_status_survives_the_cut(tmp_path):
    producer = {"query": "select " + "x, " * 1000, "complete": False, "error": "timeout"}
    _, out = melt(_package(tmp_path, [], producer=producer))
    line = out.blocks[0].text.splitlines()[1]
    assert line.startswith('producer: {"complete": false, "error": "timeout", "query": "select x')
    assert line.endswith("...")


def test_content_roles_tell_previews_from_body(tmp_path):
    (tmp_path / "t.csv").write_text("a\n1\n", "utf-8")
    resources = [
        {"name": "t", "path": "t.csv"},
        {"name": "items", "data": [1, 2], "format": "json"},
        {"name": "note", "data": {"k": 1}, "format": "json"},
    ]
    _, out = melt(_package(tmp_path, resources))
    assert [b.content().get("role") for b in out.blocks] == ["metadata", "sample", "sample", None]


def test_rows_past_the_sample_are_counted(tmp_path):
    (tmp_path / "t.tsv").write_text("a\tb\n" + "".join(f"{i}\t\n" for i in range(30)), "utf-8")
    _, out = melt(_package(tmp_path, [{"name": "t", "path": "t.tsv"}]))
    lines = out.blocks[1].text.splitlines()
    assert lines[0] == "t: first 20 of 30 rows"
    # The default missing value is the empty string
    assert lines[2] == "row=1| 0 | NULL" and len(lines) == 22


def test_json_resource_and_list_rows(tmp_path):
    (tmp_path / "one.json").write_text('{"name": "김철수", "age": "3"}', "utf-8")
    (tmp_path / "many.json").write_text('[{"k": 1}, {"k": 2}]', "utf-8")
    resources = [
        {"name": "one", "path": "one.json", "format": "json"},
        {"name": "many", "path": "many.json"},
    ]
    _, out = melt(_package(tmp_path, resources))
    one, many = out.blocks[1:]
    assert one.src.cite() == "datapackage.json#one"
    assert one.text == '{"name": "김철수", "age": "3"}'
    assert many.src.cite() == "datapackage.json#many!row=1"
    assert many.text.splitlines() == ["many: 2 items", 'row=1| {"k": 1}', 'row=2| {"k": 2}']


def test_inline_data_renders_like_a_file(tmp_path):
    resources = [
        {
            "name": "records",
            "data": [{"a": 1, "b": "x"}, {"a": 2, "b": ""}],
            "schema": {"fields": [{"name": "a", "type": "integer"}, {"name": "b"}]},
        },
        {"name": "rows", "format": "csv", "data": "x,y\n1,2\n"},
        {"name": "blob", "data": {"nested": [1, 2]}},
        {"name": "notes", "format": "md", "data": "# hi"},
    ]
    _, out = melt(_package(tmp_path, resources))
    records, rows, blob = out.blocks[1:]
    assert records.text.splitlines() == [
        "records: 2 rows",
        "columns| a (integer) | b",
        "row=1| 1 | x",
        # JSON rows keep an empty string, since null marks a missing value there
        "row=2| 2 | ",
    ]
    assert rows.text.splitlines()[1:] == ["columns| x | y", "row=1| 1 | 2"]
    assert blob.src.cite() == "datapackage.json#blob" and blob.text == '{"nested": [1, 2]}'
    assert [(c.name, c.data) for c in out.children] == [("notes.md", b"# hi")]


def test_a_table_of_plain_values_stays_a_json_block(tmp_path):
    resources = [
        {"name": "nums", "type": "table", "data": [1, 2, 3]},
        {"name": "mixed", "type": "table", "data": [{"a": 1}, 2]},
        {"name": "arrays", "type": "table", "data": [["a"], [1], 2]},
    ]
    _, out = melt(_package(tmp_path, resources))
    nums, mixed, arrays = out.blocks[1:]
    assert nums.text.splitlines() == ["nums: 3 items", "row=1| 1", "row=2| 2", "row=3| 3"]
    assert mixed.text.splitlines() == ["mixed: 2 items", 'row=1| {"a": 1}', "row=2| 2"]
    # A first array is the header row, and a stray value is a one-cell row
    assert arrays.text.splitlines() == ["arrays: 2 rows", "columns| a", "row=1| 1", "row=2| 2"]


def test_jsonl_table_with_nulls_nested_values_and_types(tmp_path):
    lines = [
        {"id": "1", "x": None, "tags": ["a", "b"], "ok": True},
        {"id": "NULL", "x": {"k": 1}, "ok": False},
    ]
    (tmp_path / "result.jsonl").write_text("".join(json.dumps(r) + "\n" for r in lines), "utf-8")
    schema = {"fields": [{"name": "id", "type": "string"}, {"name": "x", "type": "any"}]}
    resources = [
        {
            "name": "result",
            "path": "result.jsonl",
            "type": "table",
            "format": "jsonl",
            "schema": schema,
        },
        # No schema and no format: the suffix picks JSON Lines and keys give the columns
        {"name": "loose", "path": "result.jsonl"},
    ]
    _, out = melt(_package(tmp_path, resources))
    result, loose = out.blocks[1:]
    assert result.src.cite() == "datapackage.json#result!row=1"
    assert result.text.splitlines() == [
        "result: 2 rows",
        "columns| id (string) | x (any)",
        "row=1| 1 | NULL",
        'row=2| "NULL" | {"k": 1}',
    ]
    assert loose.text.splitlines()[1:] == [
        "columns| id | x | tags | ok",
        'row=1| 1 | NULL | ["a", "b"] | true',
        'row=2| "NULL" | {"k": 1} | NULL | false',
    ]
    assert out.needs == []


def test_jsonl_bad_lines_are_a_need_and_parts_join(tmp_path):
    (tmp_path / "p1.ndjson").write_text('{"a": 1}\nnot json\n\n[1, 2]\n', "utf-8")
    (tmp_path / "p2.ndjson").write_text("".join(f'{{"a": {i}}}\n' for i in range(2, 26)), "utf-8")
    _, out = melt(_package(tmp_path, [{"name": "t", "path": ["p1.ndjson", "p2.ndjson"]}]))
    lines = out.blocks[1].text.splitlines()
    assert lines[:4] == ["t: first 20 of 25 rows", "columns| a", "row=1| 1", "row=2| 2"]
    assert len(lines) == 22 and out.blocks[1].src.cite() == "datapackage.json#t!row=1"
    assert out.needs == ["resource t: 2 lines not JSON objects, first at p1.ndjson line 2"]


def test_inline_jsonl_string(tmp_path):
    resource = {"name": "r", "format": "ndjson", "data": '{"a": 1}\n{"a": 2}\n'}
    _, out = melt(_package(tmp_path, [resource]))
    assert out.blocks[1].text.splitlines() == ["r: 2 rows", "columns| a", "row=1| 1", "row=2| 2"]


def test_multipart_json_tables_join_into_one(tmp_path):
    (tmp_path / "r1.json").write_text('[{"x": 1}, {"x": 2}]', "utf-8")
    (tmp_path / "r2.json").write_text('[{"x": 3}]', "utf-8")
    (tmp_path / "a1.json").write_text('[["x", "y"], [1, 2]]', "utf-8")
    (tmp_path / "a2.json").write_text('[["x", "y"], [3, 4]]', "utf-8")
    (tmp_path / "o1.json").write_text('{"k": 1}', "utf-8")
    (tmp_path / "o2.json").write_text('{"k": 2}', "utf-8")
    resources = [
        {"name": "records", "path": ["r1.json", "r2.json"], "type": "table"},
        {"name": "arrays", "path": ["a1.json", "a2.json"], "type": "table"},
        {"name": "loose", "path": ["o1.json", "o2.json"]},
    ]
    _, out = melt(_package(tmp_path, resources))
    records, arrays, loose = out.blocks[1:]
    assert records.text.splitlines() == [
        "records: 3 rows",
        "columns| x",
        *(f"row={n}| {n}" for n in (1, 2, 3)),
    ]
    assert arrays.text.splitlines() == [
        "arrays: 2 rows",
        "columns| x | y",
        "row=1| 1 | 2",
        "row=2| 3 | 4",
    ]
    # Parts that aren't one table stay a list of parts
    assert loose.text.splitlines()[1:] == ['row=1| {"k": 1}', 'row=2| {"k": 2}']


def test_multipart_path_reads_the_header_from_the_first_part(tmp_path):
    (tmp_path / "p1.csv").write_text("a,b\n1,2\n", "utf-8")
    (tmp_path / "p2.csv").write_text("3,4\n5,6\n", "utf-8")
    _, out = melt(_package(tmp_path, [{"name": "t", "path": ["p1.csv", "p2.csv"]}]))
    assert out.blocks[1].text.splitlines() == [
        "t: 3 rows",
        "columns| a | b",
        "row=1| 1 | 2",
        "row=2| 3 | 4",
        "row=3| 5 | 6",
    ]


def test_dialect_sets_delimiter_and_header(tmp_path):
    (tmp_path / "t.csv").write_text("1;2\n", "utf-8")
    resource = {
        "name": "t",
        "path": "t.csv",
        "dialect": {"delimiter": ";", "header": False},
        "schema": {"fields": [{"name": "a"}, {"name": "b"}]},
    }
    _, out = melt(_package(tmp_path, [resource]))
    assert out.blocks[1].text.splitlines()[1:] == ["columns| a | b", "row=1| 1 | 2"]


def test_unsafe_and_missing_paths_are_needs_not_failures(tmp_path):
    (tmp_path / "secret.csv").write_text("a\n1\n", "utf-8")
    (tmp_path / "pkg" / "ok.csv").parent.mkdir()
    (tmp_path / "pkg" / "ok.csv").write_text("a\n1\n", "utf-8")
    resources = [
        {"name": "up", "path": "../secret.csv"},
        {"name": "abs", "path": str(tmp_path / "secret.csv")},
        {"name": "web", "path": "https://example.com/x.csv"},
        {"name": "gone", "path": "gone.csv"},
        {"name": "ok", "path": "ok.csv"},
    ]
    _, out = melt(_package(tmp_path / "pkg", resources))
    assert [b.src.cite() for b in out.blocks] == ["datapackage.json", "datapackage.json#ok!row=1"]
    assert out.needs == [
        "resource up: ../secret.csv rejected, outside the package",
        f"resource abs: {tmp_path / 'secret.csv'} rejected, outside the package",
        "resource web: https://example.com/x.csv not fetched, only local files are read",
        "resource gone: gone.csv missing",
    ]


def test_hidden_files_stay_unread_like_in_a_folder_walk(tmp_path):
    (tmp_path / ".env").write_text("SECRET=1\n", "utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]\n", "utf-8")
    resources = [
        {"name": "env", "path": ".env", "format": "csv"},
        {"name": "git", "path": ".git/config"},
        {"name": "schema", "data": [], "schema": ".env"},
    ]
    _, out = melt(_package(tmp_path, resources))
    assert len(out.blocks) == 1 and out.children == []
    assert out.needs == [
        "resource env: .env skipped, hidden file",
        "resource git: .git/config skipped, hidden file",
        "resource schema: .env skipped, hidden file",
    ]


def test_a_symlink_out_of_the_package_is_rejected(tmp_path):
    (tmp_path / "secret.csv").write_text("a\n1\n", "utf-8")
    path = _package(tmp_path / "pkg", [{"name": "s", "path": "link.csv"}])
    (tmp_path / "pkg" / "link.csv").symlink_to(tmp_path / "secret.csv")
    _, out = melt(path)
    assert out.needs == ["resource s: link.csv rejected, outside the package"]


def test_other_resource_files_become_children(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "scan.pdf").write_bytes(b"%PDF-1.7\n")
    path = _package(tmp_path, [{"name": "scan", "path": "docs/scan.pdf", "format": "pdf"}])
    _, out = melt(path)
    assert len(out.blocks) == 1
    [child] = out.children
    assert child.name == "docs/scan.pdf" and child.data == b"%PDF-1.7\n" and child.path is None
    assert child.parent.inside(child.name).cite() == "datapackage.json#att=docs/scan.pdf"


def test_plain_json_and_broken_descriptors_stay_text(tmp_path):
    (tmp_path / "foo.json").write_text('{"resources": []}', "utf-8")
    assert pick(tmp_path / "foo.json")[0] == "text"
    (tmp_path / "DataPackage.JSON").write_text("{not json", "utf-8")
    kind, out = melt(tmp_path / "DataPackage.JSON")
    assert kind == "datapackage"
    assert out.blocks[0].text == "1| {not json"
    assert out.needs[0].startswith("not a Data Package: invalid JSON")


def test_a_file_is_listed_once_per_resource_and_handed_on_once(tmp_path):
    (tmp_path / "a.csv").write_text("x\n1\n", "utf-8")
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.7\n")
    resources = [
        {"name": "a", "path": "a.csv"},
        {"name": "twice", "path": ["doc.pdf", "doc.pdf"], "format": "pdf"},
        {"name": "again", "path": "a.csv"},
        {"name": "doc", "path": "doc.pdf"},
        {"name": "doc-again", "path": "doc.pdf"},
    ]
    _, out = melt(_package(tmp_path, resources))
    assert [c.name for c in out.children] == ["doc.pdf"]
    # Two resources may read one table, each under its own schema
    assert [b.src.cite() for b in out.blocks[1:]] == [
        "datapackage.json#a!row=1",
        "datapackage.json#again!row=1",
    ]
    assert out.needs == [
        "resource twice: doc.pdf listed twice",
        "resource doc-again: doc.pdf already read",
    ]


def test_held_bytes_share_one_package_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(datapackage, "MAX_EMBED_BYTES", 10)
    (tmp_path / "a.txt").write_bytes(b"123456")
    (tmp_path / "b.txt").write_bytes(b"123456")
    (tmp_path / "big.csv").write_text("x\n" + "1\n" * 20, "utf-8")
    resources = [
        {"name": "a", "path": "a.txt", "format": "txt"},
        {"name": "b", "path": "b.txt", "format": "txt"},
        # CSV streams, so it never draws on the budget
        {"name": "big", "path": "big.csv"},
    ]
    _, out = melt(_package(tmp_path, resources))
    assert [c.name for c in out.children] == ["a.txt"]
    assert out.blocks[1].text.startswith("big: 20 rows")
    assert out.needs == ["resource b: over the 10 bytes package limit"]


def test_an_arrow_batch_past_the_package_budget_names_the_package_limit(tmp_path, monkeypatch):
    pa = pytest.importorskip("pyarrow")
    ipc = pytest.importorskip("pyarrow.ipc")
    monkeypatch.setattr(datapackage, "MAX_EMBED_BYTES", 10)
    # Only compressed batches unpack on the heap and draw on the budget
    table = pa.table({"x": list(range(100))})
    with ipc.new_file(
        tmp_path / "t.arrow", table.schema, options=ipc.IpcWriteOptions(compression="zstd")
    ) as w:
        w.write_table(table)
    _, out = melt(_package(tmp_path, [{"name": "t", "path": "t.arrow"}]))
    assert out.needs == ["resource t: batch 1 not read, it unpacks past the 10 bytes package limit"]


def test_a_folder_claims_csv_resources_without_pyarrow(tmp_path):
    (tmp_path / "a.csv").write_text("x\n1\n", "utf-8")
    (tmp_path / "schema.json").write_text('{"fields": [{"name": "x"}]}', "utf-8")
    (tmp_path / "other.csv").write_text("y\n2\n", "utf-8")
    desc = _package(tmp_path, [{"name": "a", "path": "a.csv", "schema": "schema.json"}])
    files = [desc, tmp_path / "a.csv", tmp_path / "other.csv", tmp_path / "schema.json"]
    assert unclaimed(files, []) == [desc, tmp_path / "other.csv"]
    # A file the user names stays, even when a package lists it
    assert unclaimed(files, [tmp_path / "a.csv"]) == [
        desc,
        tmp_path / "a.csv",
        tmp_path / "other.csv",
    ]


def test_schemas_and_dialects_are_claimed_only_when_their_resource_reads_them(tmp_path):
    (tmp_path / "a.csv").write_text("x\n1\n", "utf-8")
    (tmp_path / "a.jsonl").write_text('{"x": 1}\n', "utf-8")
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.7\n")
    names = ["csv-s", "csv-d", "jsonl-s", "jsonl-d", "gone-s", "pdf-s", "pdf-d"]
    for name in names:
        (tmp_path / f"{name}.json").write_text("{}", "utf-8")
    resources = [
        {"name": "a", "path": "a.csv", "schema": "csv-s.json", "dialect": "csv-d.json"},
        {"name": "j", "path": "a.jsonl", "schema": "jsonl-s.json", "dialect": "jsonl-d.json"},
        {"name": "gone", "path": "gone.csv", "schema": "gone-s.json"},
        {"name": "doc", "path": "doc.pdf", "schema": "pdf-s.json", "dialect": "pdf-d.json"},
    ]
    desc = _package(tmp_path, resources)
    files = [tmp_path / f"{name}.json" for name in names]
    kept = ["jsonl-d", "gone-s", "pdf-s", "pdf-d"]
    assert unclaimed([desc, *files], []) == [desc, *(tmp_path / f"{n}.json" for n in kept)]


def test_files_the_descriptor_turns_down_up_front_stay_in_the_walk(tmp_path, monkeypatch):
    # Room for the descriptor, not for the JSON part
    monkeypatch.setattr(datapackage, "MAX_PART_BYTES", 200)
    (tmp_path / "big.json").write_text(json.dumps({"k": "x" * 300}), "utf-8")
    (tmp_path / "odd.csv").write_text("x\n1\n", "utf-8")
    resources = [
        {"name": "big", "path": "big.json"},
        {"name": "odd", "path": "odd.csv", "encoding": "no-such-codec"},
    ]
    desc = _package(tmp_path, resources)
    files = [desc, tmp_path / "big.json", tmp_path / "odd.csv"]
    assert unclaimed(files, []) == files
    _, out = melt(desc)
    assert out.needs == [
        "resource big: big.json is 309 bytes, over the 200 bytes limit",
        "resource odd: unknown encoding no-such-codec",
    ]


def _arrow_file(path: Path, table) -> Path:
    ipc = pytest.importorskip("pyarrow.ipc")
    with ipc.new_file(path, table.schema) as w:
        w.write_table(table)
    return path


def _arrow_stream(path: Path, table) -> Path:
    ipc = pytest.importorskip("pyarrow.ipc")
    with ipc.new_stream(path, table.schema) as w:
        w.write_table(table)
    return path


def test_arrow_resources_melt_each_under_their_name(tmp_path):
    pa = pytest.importorskip("pyarrow")
    _arrow_stream(
        tmp_path / "result.arrows", pa.table({"id": [1, 2, 3]}).replace_schema_metadata({"k": "v"})
    )
    _arrow_stream(tmp_path / "result-2.arrows", pa.table({"k": ["a"]}))
    _arrow_file(tmp_path / "p1.arrow", pa.table({"k": [1]}))
    _arrow_file(tmp_path / "p2.arrow", pa.table({"k": [2]}))
    _arrow_file(tmp_path / "p3.arrow", pa.table({"k": [3]}))
    (tmp_path / "fake.arrow").write_bytes(b"not arrow at all")
    resources = [
        {"name": "result", "path": "result.arrows", "format": "arrows"},
        {"name": "result-2", "path": "result-2.arrows"},
        {"name": "parts", "path": ["p1.arrow", "p2.arrow"], "format": "arrow"},
        {"name": "mixed", "path": ["p3.arrow", "result-2.arrows"], "format": "arrow"},
        {"name": "fake", "path": "fake.arrow"},
    ]
    _, out = melt(_package(tmp_path, resources))
    cites = [b.src.cite() for b in out.blocks]
    assert cites == [
        "datapackage.json",
        *["datapackage.json#result"] * 2,
        "datapackage.json#result!row=1",
        "datapackage.json#result-2",
        "datapackage.json#result-2!row=1",
        "datapackage.json#parts",
        "datapackage.json#parts!row=1",
    ]
    assert out.blocks[-1].text.splitlines()[2:] == ["row=1| 1", "row=2| 2"]
    assert out.needs[0] == "resource mixed: parts have different Arrow schemas"
    assert out.needs[1].startswith("resource fake: not an Arrow IPC file or stream")


def test_an_arrow_error_of_any_kind_is_a_resource_need(tmp_path, monkeypatch):
    pa = pytest.importorskip("pyarrow")
    from meltify.converters import arrow

    def fail(*_):
        raise pa.ArrowNotImplementedError("no kernel")

    monkeypatch.setattr(arrow, "blocks", fail)
    _arrow_file(tmp_path / "t.arrow", pa.table({"k": [1]}))
    _, out = melt(_package(tmp_path, [{"name": "t", "path": "t.arrow"}]))
    assert len(out.blocks) == 1
    assert out.needs == ["resource t: unreadable Arrow: no kernel"]


def test_missing_pyarrow_is_a_need_for_the_arrow_resource_alone(tmp_path, monkeypatch):
    (tmp_path / "t.arrow").write_bytes(b"ARROW1\x00\x00")
    (tmp_path / "a.csv").write_text("x\n1\n", "utf-8")
    resources = [{"name": "t", "path": "t.arrow"}, {"name": "a", "path": "a.csv"}]
    desc = _package(tmp_path, resources)
    monkeypatch.setitem(sys.modules, "pyarrow", None)
    _, out = melt(desc)
    assert [b.src.cite() for b in out.blocks] == ["datapackage.json", "datapackage.json#a!row=1"]
    assert out.needs == ["resource t: needs pyarrow, meltify doctor --install parquet"]
    assert unclaimed([desc, tmp_path / "a.csv", tmp_path / "t.arrow"], []) == [desc]


def test_claims_spend_what_arrow_unpacks_like_the_read_does(tmp_path, monkeypatch):
    pa = pytest.importorskip("pyarrow")
    ipc = pytest.importorskip("pyarrow.ipc")
    monkeypatch.setattr(datapackage, "MAX_EMBED_BYTES", 10000)
    table = pa.table({"k": [0] * 1000})
    options = ipc.IpcWriteOptions(compression="zstd")
    with ipc.new_file(tmp_path / "t.arrow", table.schema, options=options) as w:
        w.write_table(table)
    (tmp_path / "b.txt").write_bytes(b"x" * 5000)
    resources = [{"name": "t", "path": "t.arrow"}, {"name": "b", "path": "b.txt", "format": "txt"}]
    desc = _package(tmp_path, resources)
    _, out = melt(desc)
    assert out.needs == ["resource b: over the 10000 bytes package limit"]
    # The budget turned the file down up front, so it stays in the walk
    files = [desc, tmp_path / "b.txt", tmp_path / "t.arrow"]
    assert unclaimed(files, []) == [desc, tmp_path / "b.txt"]


def test_a_folder_melts_resource_files_only_through_the_descriptor(tmp_path, monkeypatch, capsys):
    pa = pytest.importorskip("pyarrow")
    pkg = tmp_path / "pkg"
    _package(pkg, [{"name": "result", "path": "result.arrows"}])
    _arrow_stream(pkg / "result.arrows", pa.table({"id": [1, 2, 3]}))
    (pkg / "notes.txt").write_text("not a resource")
    monkeypatch.chdir(tmp_path)
    assert main(["read", "pkg", "--json", "--limit", "0", "--out", "out"]) == 0
    rows = json.loads(capsys.readouterr().out)["results"]
    assert sorted(r["cite"] for r in rows) == ["pkg/datapackage.json", "pkg/notes.txt"]


def test_paths_that_cannot_resolve_are_needs_in_reads_and_claims(tmp_path):
    (tmp_path / "ok.csv").write_text("x\n1\n", "utf-8")
    (tmp_path / "other.csv").write_text("y\n2\n", "utf-8")
    (tmp_path / "loop.csv").symlink_to("loop.csv")
    resources = [
        {"name": "nul", "path": "a\u0000.csv"},
        {"name": "loop", "path": "loop.csv"},
        {"name": "schema-loop", "path": "ok.csv", "schema": "loop.csv"},
        {"name": "ok", "path": "ok.csv"},
    ]
    desc = _package(tmp_path, resources)
    # One bad resource can't take down the folder walk or the claims of the others
    files = [desc, tmp_path / "ok.csv", tmp_path / "other.csv"]
    assert unclaimed(files, []) == [desc, tmp_path / "other.csv"]
    _, out = melt(desc)
    assert [b.src.cite() for b in out.blocks] == ["datapackage.json", "datapackage.json#ok!row=1"]
    assert [n.split(":")[0] for n in out.needs] == [
        "resource nul",
        "resource loop",
        "resource schema-loop",
    ]


def test_json_nested_past_the_recursion_limit_is_a_need(tmp_path):
    (tmp_path / "deep.json").write_text("[" * 100000, "utf-8")
    resources = [
        {"name": "deep", "path": "deep.json"},
        {"name": "inline", "format": "json", "data": "[" * 100000},
        {"name": "ok", "data": [1]},
    ]
    _, out = melt(_package(tmp_path, resources))
    assert [b.src.cite() for b in out.blocks] == ["datapackage.json", "datapackage.json#ok!row=1"]
    assert [n.split(":")[0] for n in out.needs] == ["resource deep", "resource inline"]
    deep = tmp_path / "deep" / "datapackage.json"
    deep.parent.mkdir()
    deep.write_text("[" * 100000, "utf-8")
    assert unclaimed([deep], []) == [deep]
    kind, out = melt(deep)
    assert kind == "datapackage" and out.needs[0].startswith("not a Data Package: invalid JSON")


def test_an_arrow_resource_past_python_datetime_range_still_melts(tmp_path):
    pa = pytest.importorskip("pyarrow")
    table = pa.table({"t": pa.array([400_000_000_000, 1], pa.timestamp("s"))})
    _arrow_file(tmp_path / "t.arrow", table)
    _, out = melt(_package(tmp_path, [{"name": "t", "path": "t.arrow"}]))
    assert out.needs == []
    assert "| t | timestamp[s] | 0 | 1970-01-01 00:00:01 | 14645-06-30 15:06:40 | 2 |" in (
        out.blocks[1].text
    )


def test_a_resource_reads_as_one_kind_and_is_a_table_or_not_on_its_own():
    assert datapackage._kind({"path": "a.txt", "schema": {}}) == "csv"
    assert datapackage._kind({"path": "a.txt"}) == "file"
    assert datapackage._kind({"path": "a.json", "type": "table"}) == "json"
    assert datapackage._tabular({"path": "a.json", "type": "table"})
    assert not datapackage._tabular({"path": "a.json"})
    assert datapackage._tabular({"path": "a.ndjson"})


def test_a_json_value_renders_from_its_schema_alone(tmp_path):
    out = datapackage.Converted("datapackage")
    reader = datapackage.Reader(tmp_path, Src("datapackage.json"), out)
    schema = {"fields": [{"name": "a", "type": "integer"}]}
    reader._value("t", [{"a": 1}], True, schema)
    assert out.blocks[0].text.splitlines() == ["t: 1 row", "columns| a (integer)", "row=1| 1"]


def test_only_shown_rows_are_cleaned(tmp_path, monkeypatch):
    cleaned = []
    clean = datapackage.Table.clean

    def counting(self, row):
        cleaned.append(row)
        return clean(self, row)

    monkeypatch.setattr(datapackage.Table, "clean", counting)
    (tmp_path / "t.csv").write_text("a\n" + "1\n" * 30, "utf-8")
    _, out = melt(_package(tmp_path, [{"name": "t", "path": "t.csv"}]))
    assert out.blocks[1].text.startswith("t: first 20 of 30 rows")
    assert len(cleaned) == 20


def test_the_arrow_read_asks_the_arrow_module_for_pyarrow(tmp_path, monkeypatch):
    from meltify.converters import arrow
    from meltify.safe import MissingTool

    def missing():
        raise MissingTool("pyarrow", "hint")

    monkeypatch.setattr(arrow, "require", missing)
    (tmp_path / "t.arrow").write_bytes(b"ARROW1\x00\x00")
    _, out = melt(_package(tmp_path, [{"name": "t", "path": "t.arrow"}]))
    assert out.needs == ["resource t: needs pyarrow, hint"]


def test_a_field_without_a_name_keeps_its_type():
    table = datapackage.Table({"fields": [{"type": "integer"}, {"name": "b", "type": "string"}]})
    assert table.names == ["", "b"]
    assert [table.label(n) for n in table.names] == [" (integer)", "b (string)"]


def test_one_row_and_one_item_are_singular(tmp_path):
    (tmp_path / "one.csv").write_text("a\n1\n", "utf-8")
    resources = [{"name": "one", "path": "one.csv"}, {"name": "item", "data": [1]}]
    _, out = melt(_package(tmp_path, resources))
    assert [b.text.splitlines()[0] for b in out.blocks[1:]] == ["one: 1 row", "item: 1 item"]


def test_a_codec_that_isnt_a_text_encoding_is_a_need_and_stays_unclaimed(tmp_path):
    (tmp_path / "a.csv").write_text("x\n1\n", "utf-8")
    (tmp_path / "a.json").write_text("[1]", "utf-8")
    (tmp_path / "b.csv").write_text("y\n2\n", "utf-8")
    resources = [
        {"name": "rot", "path": "a.csv", "encoding": "rot13"},
        {"name": "b64", "path": "a.json", "encoding": "base64"},
        {"name": "ok", "path": "b.csv"},
    ]
    path = _package(tmp_path, resources)
    _, out = melt(path)
    assert [b.src.cite() for b in out.blocks] == ["datapackage.json", "datapackage.json#ok!row=1"]
    assert out.needs == [
        "resource rot: rot13 is not a text encoding",
        "resource b64: base64 is not a text encoding",
    ]
    # Turned down before any read, so both files still melt on their own in a folder read
    assert datapackage.claims(path) == {(tmp_path / "b.csv").resolve()}


def test_a_lookup_error_while_reading_is_a_need(tmp_path, monkeypatch):
    (tmp_path / "a.json").write_text("[1]", "utf-8")
    path = _package(tmp_path, [{"name": "a", "path": "a.json"}])

    def failing(target, encoding):
        raise LookupError("codec vanished")

    monkeypatch.setattr(datapackage, "_load_json", failing)
    out = datapackage.convert(path, Src(path.name))
    assert out.needs == ["resource a: codec vanished"]


def test_a_repeated_table_read_draws_on_the_package_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(datapackage, "MAX_EMBED_BYTES", 30)
    (tmp_path / "t.csv").write_text("x\n" + "1\n" * 6, "utf-8")
    (tmp_path / "t.jsonl").write_text('{"x": 1}\n', "utf-8")
    resources = [
        # A first read streams, so it's free
        {"name": "first", "path": "t.csv"},
        {"name": "rows", "path": "t.jsonl"},
        # 14 bytes each from here on
        {"name": "second", "path": "t.csv"},
        {"name": "third", "path": "t.csv"},
        {"name": "fourth", "path": "t.csv"},
    ]
    path = _package(tmp_path, resources)
    _, out = melt(path)
    assert [b.src.sheet for b in out.blocks[1:]] == ["first", "rows", "second", "third"]
    assert out.needs == ["resource fourth: over the 30 bytes package limit"]
    # Claims spend the same budget, so they claim what the reads that pass read
    assert datapackage.claims(path) == {(tmp_path / n).resolve() for n in ("t.csv", "t.jsonl")}


def test_dialect_quote_escape_comment_and_initial_space(tmp_path):
    (tmp_path / "t.csv").write_text("a, b\n# note\n'x, y', z\\,w\n", "utf-8")
    dialect = {"quoteChar": "'", "escapeChar": "\\", "commentChar": "#", "skipInitialSpace": True}
    resource = {"name": "t", "path": "t.csv", "dialect": dialect}
    _, out = melt(_package(tmp_path, [resource]))
    assert out.blocks[1].text.splitlines()[1:] == ["columns| a | b", "row=1| x, y | z,w"]


def test_a_bad_schema_or_path_is_a_need(tmp_path):
    (tmp_path / "t.csv").write_text("a\n1\n", "utf-8")
    (tmp_path / "broken.json").write_text("{not json", "utf-8")
    (tmp_path / "list.json").write_text("[]", "utf-8")
    resources = [
        {"name": "broken", "path": "t.csv", "schema": "broken.json"},
        {"name": "list", "path": "t.csv", "schema": "list.json"},
        {"name": "inline", "path": "t.csv", "schema": ["a"]},
        {"name": "number", "path": 5},
    ]
    _, out = melt(_package(tmp_path, resources))
    assert len(out.blocks) == 1
    assert out.needs[0].startswith("resource broken: schema broken.json is not JSON: ")
    assert out.needs[1:] == [
        "resource list: schema is not an object",
        "resource inline: schema is not an object",
        "resource number: path 5 is not a string",
    ]


def test_a_schema_file_over_the_part_limit_is_turned_down(tmp_path, monkeypatch):
    (tmp_path / "big.json").write_text('{"fields": []}' + " " * 40, "utf-8")
    monkeypatch.setattr(datapackage, "MAX_PART_BYTES", 40)
    with pytest.raises(datapackage.Skip, match="^schema big.json over the 40 bytes limit$"):
        datapackage._object(tmp_path, "big.json", "schema")
