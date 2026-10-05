import json
import shutil
from pathlib import Path

from meltify.cli import main
from meltify.commands.brief import extract

STATEMENT = """# 주차 시뮬레이터 과제

API로 시뮬레이터에 주차 경로를 보냅니다.
10연승을 달성하면 통과입니다.
제출은 1분에 1회로 제한되며 같은 이미지는 다시 제출할 수 없습니다.
기준일은 2026-03-14이고 시간대는 KST입니다.
위반이 여러 개면 가장 낮은 규칙 번호를 사유로 제출하세요.
답은 대문자만 사용한 JSON으로 제출합니다.
제공 데이터에 없는 내용은 추측하지 마세요.
Answer with exactly 5 words.
"""


def test_extract_quotes_conditions_with_line_numbers():
    rows = {r["src"]["line"]: r for r in extract(STATEMENT, "p.md")}
    assert rows[4]["quote"] == "10연승을 달성하면 통과입니다."
    assert "count" in rows[4]["kinds"]
    assert {"limit"} <= set(rows[5]["kinds"])
    assert {"date", "timezone"} <= set(rows[6]["kinds"])
    assert "priority" in rows[7]["kinds"]
    assert "format" in rows[8]["kinds"]
    assert "prohibition" in rows[9]["kinds"]
    assert {"count", "format"} <= set(rows[10]["kinds"])
    assert rows[4]["cite"] == "p.md:4"
    assert 3 not in rows


def test_new_board_and_set(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "p.md").write_text(STATEMENT, encoding="utf-8")
    assert main(["brief", "new", "4", "주차 AI", "--from", "p.md"]) == 0
    notes = Path("problems/04-주차-AI/NOTES.md").read_text()
    assert "- [ ] 10연승을 달성하면 통과입니다.  `p.md:4`" in notes
    capsys.readouterr()

    assert main(["brief", "set", "4", "--state", "doing", "--points", "30", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    row = out["results"][0]
    assert row["state"] == "doing" and row["points"] == 30
    assert row["checks"].startswith("0/")

    assert main(["brief", "board"]) == 0
    assert "04-주차-AI" in capsys.readouterr().out
    assert main(["brief", "set", "9", "--state", "done"]) == 2


def test_korean_template(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "meltify.toml").write_text('[brief]\ntemplate = "ko"\n')
    assert main(["brief", "new", "1", "메뉴"]) == 0
    assert "## 체크리스트" in Path("problems/01-메뉴/NOTES.md").read_text()


def test_new_refuses_existing_number_without_force(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["brief", "new", "2", "first"]) == 0
    notes = Path("problems/02-first/NOTES.md")
    notes.write_text(notes.read_text() + "- [x] checked\n")
    assert main(["brief", "new", "2", "first"]) == 2
    assert main(["brief", "new", "2", "other"]) == 2
    assert "- [x] checked" in notes.read_text()
    assert sorted(p.name for p in Path("problems").iterdir()) == ["02-first"]
    assert main(["brief", "new", "2", "first", "--force"]) == 0
    assert "- [x] checked" not in notes.read_text()


def test_set_refuses_ambiguous_number(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["brief", "new", "3", "a"]) == 0
    shutil.copytree("problems/03-a", "problems/03-b")
    assert main(["brief", "set", "3", "--state", "done"]) == 2


def test_bad_template_leaves_no_empty_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "meltify.toml").write_text('[brief]\ntemplate = "nope"\n')
    assert main(["brief", "new", "1", "x"]) == 2
    assert not Path("problems").exists() or not any(Path("problems").iterdir())


def test_board_skips_dirs_without_status(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["brief", "new", "1", "ok"]) == 0
    Path("problems/stray").mkdir()
    capsys.readouterr()
    assert main(["brief", "board", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [r["problem"] for r in out["results"]] == ["01-ok"]
    assert any("stray" in w for w in out["warnings"])
