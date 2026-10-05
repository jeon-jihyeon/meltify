import json

import pytest

from meltify.cli import main
from meltify.commands.check import Checker, Rule, select

SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "required": ["id", "answer"],
        "properties": {
            "id": {"type": "string", "pattern": "^item_[0-9]{3}$"},
            "answer": {"enum": ["Approve", "Deny"]},
            "reason": {"type": ["integer", "null"]},
        },
        # A cross-field rule written in plain JSON Schema instead of a custom DSL
        "if": {"properties": {"answer": {"const": "Approve"}}},
        "then": {"properties": {"reason": {"type": "null"}}},
    },
}


def _rules(found):
    return sorted((f["rule"], f["cite"]) for f in found)


def test_answer_file_violations():
    data = [
        {"id": "item_001", "answer": "Approve", "reason": None},
        {"id": "item_001", "answer": "deny", "reason": 3},
        {"id": "item_003", "answer": "Approve", "reason": 7},
    ]
    found = Checker("a.json", schema=SCHEMA, count=4, unique=["id"]).run(data)
    assert _rules(found) == [
        ("count", "a.json#$"),
        ("schema", "a.json#$[1].answer"),
        ("schema", "a.json#$[2].reason"),
        ("unique", "a.json#$[1].id"),
    ]


@pytest.mark.parametrize(
    "rule,value,want",
    [
        (Rule(digits=True), "１２", ["digits"]),
        (Rule(digits=True), "12", []),
        (Rule(upper=True), "ALPHAk", ["upper"]),
        (Rule(pattern="[A-Z]+"), "ABC", []),
        (Rule(pattern="[A-Z]+"), "AB C", ["pattern"]),
        (Rule(words=5), "a b c", ["words"]),
        (Rule(max_words=2), "a b c", ["max_words"]),
        (Rule(include=("확인 불가",)), "근거 없음", ["include"]),
        (Rule(), 12, ["type"]),
    ],
)
def test_text_rules(rule, value, want):
    assert [r for r, _ in rule.problems(value)] == want


def test_hygiene_catches_whitespace_and_fullwidth():
    found = Checker("t").run({"a": " x", "b": "a  b", "c": "１２", "d": "ok"})
    assert _rules(found) == [("hygiene", "t#$.a"), ("hygiene", "t#$.b"), ("hygiene", "t#$.c")]
    assert Checker("t", check_hygiene=False).run({"a": " x"}) == []


def test_missing_path_is_reported():
    found = Checker("t", rules=[Rule(path="$.q9", pattern=".+")]).run({"q2": "A"})
    assert _rules(found) == [("path", "t#$.q9")]


def test_unique_ignores_items_without_the_key():
    assert Checker("t", unique=["id"]).run([{"x": 1}, {"x": 2}]) == []


@pytest.mark.parametrize(
    "path,want",
    [
        ("$", ["$"]),
        ("$.q2", ["$.q2"]),
        ("$.items[*].id", ["$.items[0].id", "$.items[1].id"]),
        ("$.items[-1].id", ["$.items[-1].id"]),
        ("$.nope", []),
    ],
)
def test_select(path, want):
    data = {"q2": "A", "items": [{"id": 1}, {"id": 2}]}
    assert [p for p, _ in select(data, path)] == want


def test_cli_exit_codes_and_config(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({"q2": "ALPHA", "q6": "１２", "q7": "a b c"}))
    (tmp_path / "meltify.toml").write_text(
        '[[check.field]]\npath = "$.q6"\ndigits = true\n[[check.field]]\npath = "$.q7"\nwords = 3\n'
    )
    assert main(["check", str(answers), "--json"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert {r["rule"] for r in out["results"]} == {"digits", "hygiene"}

    assert main(["check", "--text", "ALPHA", "--upper"]) == 0
    assert "no violations" in capsys.readouterr().out
    assert main(["check", str(answers), "--upper"]) == 2
    assert main(["check"]) == 2
    assert main(["check", str(answers), "--field", "$[?(@.q2)]", "--upper"]) == 2
    assert main(["check", str(answers), "--field", "q2", "--upper"]) == 2


def test_csv_and_jsonl_inputs(tmp_path):
    from meltify.commands.check import load

    (tmp_path / "a.csv").write_text("id,answer\n1,Approve\n2,Deny\n")
    (tmp_path / "a.jsonl").write_text('{"id": 1}\n\n{"id": 2}\n')
    assert load(tmp_path / "a.csv")[1] == {"id": "2", "answer": "Deny"}
    assert load(tmp_path / "a.jsonl") == [{"id": 1}, {"id": 2}]


def test_list_rules_on_non_list_are_violations():
    found = Checker("t", count=1, unique=["id"]).run({"id": 1})
    assert _rules(found) == [("count", "t#$"), ("unique", "t#$")]


def test_unique_tells_bool_from_int_and_zero_count_is_real():
    assert Checker("t", unique=["id"]).run([{"id": 1}, {"id": True}]) == []
    assert _rules(Checker("t", count=0).run([{"id": 1}])) == [("count", "t#$")]


def test_run_twice_does_not_accumulate():
    checker = Checker("t", count=2)
    assert len(checker.run([1])) == 1
    assert len(checker.run([1])) == 1


def test_ignored_or_broken_rules_are_usage_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    answers = tmp_path / "a.json"
    answers.write_text(json.dumps([{"id": "A"}]))
    assert main(["check", "--text", "A", "--count", "1"]) == 2
    assert main(["check", "--text", "A", "--unique", "id"]) == 2
    assert main(["check", "--text", "A", "--schema", "s.json"]) == 2
    assert main(["check", str(answers), "--field", "$[*].id"]) == 2
    capsys.readouterr()
    assert main(["check", "--text", "A", "--pattern", "("]) == 2
    assert "Traceback" not in capsys.readouterr().err


def test_config_count_zero_is_enforced(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    (tmp_path / "a.json").write_text("[1]")
    (tmp_path / "meltify.toml").write_text("[check]\ncount = 0\n")
    assert main(["check", "a.json"]) == 1
