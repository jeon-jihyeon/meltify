import io
import json

import pytest

from meltify import cli, files
from meltify.evidence import Envelope
from meltify.output import append_jsonl, emit, read_jsonl, table


def test_table_aligns_and_truncates():
    out = table([{"a": "x", "b": "y" * 100}, {"a": "long", "b": None}], ["a", "b"])
    lines = out.splitlines()
    assert lines[0].startswith("a     b")
    assert lines[1].endswith("…")
    assert lines[2] == "long"


def test_table_aligns_wide_characters():
    out = table([{"a": "가나", "b": "x"}, {"a": "abcd", "b": "y"}], ["a", "b"])
    lines = out.splitlines()
    assert lines[1] == "가나  x"
    assert lines[2] == "abcd  y"


def test_negative_limit_is_a_usage_error():
    parser = cli.build_parser(cli._modules())
    with pytest.raises(SystemExit) as e:
        parser.parse_args(["doctor", "--limit", "-1"])
    assert e.value.code == 2
    assert parser.parse_args(["doctor", "--limit", "0"]).limit == 0


def test_emit_limit_points_to_artifact():
    env = Envelope("x", "0")
    env.results = [{"v": i} for i in range(5)]
    env.artifact("out/results.jsonl", "results")
    buf = io.StringIO()
    emit(env, as_json=False, columns=["v"], limit=2, out=buf, err=io.StringIO())
    text = buf.getvalue()
    assert "3 more rows, see out/results.jsonl" in text


def test_emit_json_is_full_envelope():
    env = Envelope("x", "0")
    env.results = [{"v": 1}]
    buf = io.StringIO()
    emit(env, as_json=True, columns=["v"], limit=0, out=buf)
    assert json.loads(buf.getvalue())["results"] == [{"v": 1}]


def test_jsonl_roundtrip(tmp_path):
    p = tmp_path / "d/log.jsonl"
    append_jsonl(p, [{"k": "가"}])
    append_jsonl(p, [{"k": 2}])
    assert read_jsonl(p) == [{"k": "가"}, {"k": 2}]
    assert read_jsonl(tmp_path / "none.jsonl") == []


def test_iter_files_skips_hidden_and_filters(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a/x.PDF").write_text("1")
    (tmp_path / "a/y.txt").write_text("1")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git/z.pdf").write_text("1")
    got = list(files.iter_files([tmp_path], {".pdf"}))
    assert [p.name for p in got] == ["x.PDF"]


def test_flat_name_and_root(tmp_path):
    f = tmp_path / "in/mail box/a b.eml"
    f.parent.mkdir(parents=True)
    f.write_text("x")
    assert files.flat_name(f, tmp_path / "in") == "mail_box__a_b.eml"
    assert files.common_root([f, tmp_path / "in"]) == (tmp_path / "in").resolve()
    assert len(files.sha256(f)) == 64
