import json
import sqlite3
from pathlib import Path

import pytest

import meltify
from examples.data_consumer import STATUSES, main, read_events, read_matches, read_package


def package(root, outcome, rows=2):
    root.mkdir()
    (root / "result.csv").write_text("value\n" + "\n".join(str(i) for i in range(rows)))
    path = root / "datapackage.json"
    path.write_text(
        json.dumps(
            {
                "gatereach": {"query": "PRIVATE_QUERY", "user": "PRIVATE_ACCOUNT", **outcome},
                "resources": [{"name": "result", "path": "result.csv", "format": "csv"}],
            }
        )
    )
    return path


@pytest.mark.parametrize(
    ("outcome", "ready"),
    [
        ({"complete": True, "limit": 1000, "follow": False}, True),
        ({"complete": True, "follow": True}, True),
        ({"complete": False, "error": "PRIVATE_ERROR"}, False),
        ({"complete": True, "error": "PRIVATE_ERROR"}, False),
        ({"complete": True, "stopped": "budget"}, False),
        ({"complete": True, "stopped": "PRIVATE_STOP"}, False),
        ({"complete": "true"}, False),
        ({}, False),
    ],
)
def test_source_outcome_and_read_coverage_are_independent(tmp_path, outcome, ready):
    source = package(tmp_path / "pkg", outcome)
    result = read_package(source, tmp_path / "out")
    assert result["ready"] is ready
    assert result["read"] == {
        "schema": "meltify/v1",
        "version": meltify.__version__,
        "ok": True,
        "complete": True,
        "coverage": ["complete"],
    }
    assert result["connector"]["ok"] is ready
    assert result["descriptor_unchanged"]
    assert "PRIVATE_" not in json.dumps(result)


@pytest.mark.parametrize("rows", [0, 1, 21])
def test_projection_keeps_original_cites_and_declares_sample(tmp_path, rows):
    source = package(tmp_path / "pkg", {"complete": True}, rows)
    result = read_package(source, tmp_path / "out")
    assert result["ready"]
    assert not result["full_rows"]
    assert result["representation"] == "summary_and_sample"
    [document] = result["connector"]["documents"]
    reference = result["source_map"][document["docId"]]
    assert document["metadata"]["evidenceLocation"] == reference["cite"]
    assert reference["src"]["path"] == str(source)
    assert "row=21|" not in document["content"]
    assert "PRIVATE_" not in document["content"]
    assert "query" not in document["metadata"]


def test_modified_and_deleted_blocks_keep_snapshot_identity(tmp_path):
    source = package(tmp_path / "pkg", {"complete": True})
    first = read_package(source, tmp_path / "out")
    (source.parent / "result.csv").write_text("value\nchanged")
    second = read_package(source, tmp_path / "out")
    assert first["source_map"].keys() == second["source_map"].keys()
    [key] = first["source_map"]
    assert first["source_map"][key]["sha256"] != second["source_map"][key]["sha256"]
    descriptor = json.loads(source.read_text())
    descriptor["resources"] = []
    source.write_text(json.dumps(descriptor))
    third = read_package(source, tmp_path / "out")
    assert third["ready"]
    assert third["connector"]["documents"] == []
    assert third["source_map"] == {}


def test_relative_package_paths_keep_original_cites(tmp_path, monkeypatch):
    source = package(tmp_path / "pkg", {"complete": True})
    monkeypatch.chdir(tmp_path)
    result = read_package(source.relative_to(tmp_path), Path("out"))
    assert result["ready"]
    assert all(
        ref["src"]["path"] == "pkg/datapackage.json" for ref in result["source_map"].values()
    )


@pytest.mark.parametrize(
    ("cap", "unit", "expected"),
    [
        (2.5, "read capacity units", {"max": 2.5, "unit": "read capacity units"}),
        (float("inf"), "PRIVATE_UNIT", {}),
        (True, "PRIVATE_UNIT", {}),
        (-1, [], {}),
    ],
)
def test_budget_preserves_original_units_without_diagnostics(tmp_path, cap, unit, expected):
    source = package(tmp_path / "pkg", {"complete": True, "max": cap, "unit": unit})
    result = read_package(source, tmp_path / "out")
    assert result["source"] == {"complete": True, **expected}
    assert "PRIVATE_" not in json.dumps(result)


@pytest.mark.parametrize(
    "resources",
    [
        [{"name": "memo", "data": "hello", "format": "txt"}],
        [{"path": "result.csv", "format": "csv"}],
        [{"name": "result", "path": "result.csv"}],
        [{"name": "result", "path": "result.csv", "format": "csv"}] * 2,
        [{"name": "result", "path": "result.csv", "format": ["csv"]}],
        [{"name": ["result"], "path": "result.csv", "format": "csv"}],
    ],
)
def test_unsupported_resources_cannot_silently_become_an_empty_snapshot(tmp_path, resources):
    source = package(tmp_path / "pkg", {"complete": True})
    descriptor = json.loads(source.read_text())
    descriptor["resources"] = resources
    source.write_text(json.dumps(descriptor))
    result = read_package(source, tmp_path / "out")
    assert result["read"]["ok"] and result["read"]["complete"]
    assert not result["ready"] and not result["resources_supported"]
    assert result["connector"] == {"ok": False, "reason": "invalid-data"}


@pytest.mark.parametrize("shallow", [True, False])
def test_native_table_content_keeps_representation_roles(tmp_path, shallow):
    source = tmp_path / "data.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE items(value TEXT)")
        connection.execute("INSERT INTO items VALUES ('ann')")
    env = meltify.read(source, out=tmp_path / "out", content=True, shallow=shallow)
    assert [block.get("role") for block in env.results[0]["content"]] == ["metadata", "sample"]


def test_parquet_content_keeps_representation_roles(tmp_path):
    pa = pytest.importorskip("pyarrow")
    import pyarrow.parquet as pq

    source = tmp_path / "data.parquet"
    pq.write_table(pa.table({"value": ["ann"]}), source)
    env = meltify.read(source, out=tmp_path / "out", content=True, shallow=True)
    assert [block.get("role") for block in env.results[0]["content"]] == [
        "metadata",
        "summary",
        "sample",
    ]


def test_unread_resource_does_not_become_ready(tmp_path):
    source = package(tmp_path / "pkg", {"complete": True})
    (source.parent / "result.csv").unlink()
    result = read_package(source, tmp_path / "out")
    assert not result["ready"]
    assert result["read"]["ok"] and not result["read"]["complete"]
    assert result["source_map"] == {}


def test_changed_descriptor_is_not_a_coherent_snapshot(tmp_path, monkeypatch):
    source = package(tmp_path / "pkg", {"complete": True})
    original = meltify.read

    def read(*args, **kwargs):
        result = original(*args, **kwargs)
        source.write_text(source.read_text() + "\n")
        return result

    monkeypatch.setattr(meltify, "read", read)
    result = read_package(source, tmp_path / "out")
    assert not result["ready"] and not result["descriptor_unchanged"]
    assert result["connector"] == {"ok": False, "reason": "invalid-data"}


@pytest.mark.parametrize("body", ["[]", "{}", "{", "x" * ((64 << 20) + 1)])
def test_cli_reports_coarse_failure_without_source_text(tmp_path, capsys, body):
    path = tmp_path / "datapackage.json"
    path.write_text(body)
    assert main([str(path), "--out", str(tmp_path / "out")]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "ready": False,
        "connector": {"ok": False, "reason": "invalid-data"},
    }


def test_missing_input_and_success_cli(tmp_path, capsys):
    path = tmp_path / "missing.json"
    assert main([str(path), "--out", str(tmp_path / "out")]) == 1
    assert not json.loads(capsys.readouterr().out)["ready"]
    path = package(tmp_path / "pkg", {"complete": True})
    assert main([str(path), "--out", str(tmp_path / "out")]) == 0
    assert json.loads(capsys.readouterr().out)["ready"]


@pytest.mark.parametrize(
    ("values", "kept"), [([None, "9007199254740993"], "9007199254740993"), ([], "0 rows")]
)
def test_arrow_metadata_is_not_connector_evidence(tmp_path, values, kept):
    pa = pytest.importorskip("pyarrow")
    source = package(tmp_path / "pkg", {"complete": True})
    table = pa.table({"value": values}).replace_schema_metadata(
        {"gatereach": '{"query":"PRIVATE_ARROW_QUERY","user":"PRIVATE_ARROW_ACCOUNT"}'}
    )
    with (
        pa.OSFile(str(source.parent / "result.arrows"), "wb") as stream,
        pa.ipc.new_stream(stream, table.schema) as writer,
    ):
        writer.write_table(table)
    descriptor = json.loads(source.read_text())
    descriptor["resources"][0].update(path="result.arrows", format="arrow")
    source.write_text(json.dumps(descriptor))
    result = read_package(source, tmp_path / "out")
    assert result["ready"]
    assert "PRIVATE_" not in json.dumps(result)
    text = "\n".join(d["content"] for d in result["connector"]["documents"])
    assert kept in text
    assert len(result["connector"]["documents"]) == len(result["source_map"])
    env = meltify.read(source, out=tmp_path / "raw", content=True, shallow=True)
    assert [block.get("role") for block in env.results[0]["content"]] == [
        "metadata",
        "metadata",
        "summary",
        "sample",
    ]


@pytest.mark.parametrize(
    ("status", "exit_code", "rows", "error", "complete", "ready"),
    [
        ("finished", 0, 1, None, True, True),
        ("stopped", 0, 1, None, True, True),
        ("budget", 0, 1, None, True, False),
        ("error", 1, 1, "PRIVATE_ERROR", False, False),
        ("interrupted", 130, 1, None, False, False),
        ("finished", 1, 1, None, False, False),
        ("finished", 0, 2, None, False, False),
        ("finished", 0, True, None, False, False),
        ("finished", 0, 1, "PRIVATE_ERROR", False, False),
        ("PRIVATE_STATUS", 0, 1, None, False, False),
        ([], 0, 1, None, False, False),
    ],
)
def test_events_require_terminal_and_process_result(
    tmp_path, status, exit_code, rows, error, complete, ready
):
    path = tmp_path / "events.jsonl"
    items = [
        {"schema": "gatereach/v1", "type": "record", "record": {"value": None}},
        {
            "schema": "gatereach/v1",
            "type": "end",
            "status": status,
            "rows": rows,
            "error": error,
            "follow": status == "stopped",
            "limit": 1000,
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in items))
    result = read_events(path, exit_code)
    assert result["complete"] is complete and result["ready"] is ready
    assert result["status"] == (status if isinstance(status, str) and status in STATUSES else None)
    assert result["scope"] == {"limit": 1000, "follow": status == "stopped"}
    assert "PRIVATE_" not in json.dumps(result)


@pytest.mark.parametrize(
    "items",
    [
        [{"schema": "gatereach/v1", "type": "record", "record": {}}],
        [],
    ],
)
def test_eof_without_terminal_is_unconfirmed(tmp_path, items):
    path = tmp_path / "events.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in items))
    result = read_events(path, 0)
    assert not result["ready"] and not result["complete"] and not result["terminal"]


@pytest.mark.parametrize("follow", [False, None, "true"])
def test_stopped_terminal_requires_follow_scope(tmp_path, follow):
    path = tmp_path / "events.jsonl"
    path.write_text(
        json.dumps(
            {
                "schema": "gatereach/v1",
                "type": "end",
                "status": "stopped",
                "rows": 0,
                "follow": follow,
            }
        )
    )
    result = read_events(path, 0)
    assert not result["ready"] and not result["complete"]


@pytest.mark.parametrize(
    ("items", "events"),
    [
        ([[]], True),
        ([{"schema": "other", "type": "record"}], True),
        ([{"schema": "gatereach/v1", "type": "other"}], True),
        ([{"schema": "gatereach/v1", "type": "record", "record": []}], True),
        ([{"schema": "gatereach/v1", "type": "end"}] * 2, True),
        ([{"schema_version": "moldrain.match/v1", "matched": "true"}], False),
        ([{"schema_version": "other", "matched": True}], False),
    ],
)
def test_malformed_streams_are_rejected(tmp_path, items, events):
    path = tmp_path / "rows.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in items))
    with pytest.raises(ValueError):
        (read_events(path, 0) if events else read_matches(path, 0, strict=False))


@pytest.mark.parametrize(
    ("exit_code", "strict", "misses", "complete", "ready"),
    [
        (0, False, 0, True, True),
        (1, True, 1, True, False),
        (2, False, 0, False, False),
        (1, False, 1, False, False),
        (1, True, 0, False, False),
    ],
)
def test_match_rows_require_process_completion(
    tmp_path, exit_code, strict, misses, complete, ready
):
    path = tmp_path / "rows.jsonl"
    path.write_text(
        json.dumps({"schema_version": "moldrain.match/v1", "matched": not misses}) + "\n"
    )
    result = read_matches(path, exit_code, strict)
    assert result["complete"] is complete and result["ready"] is ready
    assert result["rows"] == 1 and result["misses"] == misses


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["pkg"],
        ["pkg", "--out", "out", "--exit-code", "0"],
        ["--events", "rows"],
        ["--events", "rows", "--exit-code", "0", "--strict"],
        ["--events", "rows", "--exit-code", "0", "--out", "out"],
        ["pkg", "--events", "rows", "--exit-code", "0"],
    ],
)
def test_cli_refuses_ambiguous_or_incomplete_input(args):
    with pytest.raises(SystemExit) as error:
        main(args)
    assert error.value.code == 2


@pytest.mark.parametrize("mode", ["--events", "--match"])
def test_stream_cli(tmp_path, capsys, mode):
    path = tmp_path / "rows.jsonl"
    path.write_text(
        json.dumps(
            {"schema": "gatereach/v1", "type": "end", "status": "finished", "rows": 0}
            if mode == "--events"
            else {"schema_version": "moldrain.match/v1", "matched": True}
        )
    )
    assert main([mode, str(path), "--exit-code", "0"]) == 0
    assert json.loads(capsys.readouterr().out)["ready"]


@pytest.mark.parametrize("body", ["{", "x" * 33])
def test_stream_failure_cli_keeps_diagnostics_coarse(tmp_path, capsys, monkeypatch, body):
    monkeypatch.setattr("examples.data_consumer.MAX_STREAM_CHARS", 32)
    path = tmp_path / "rows.jsonl"
    path.write_text(body)
    assert main(["--events", str(path), "--exit-code", "0"]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "ready": False,
        "connector": {"ok": False, "reason": "invalid-data"},
    }
