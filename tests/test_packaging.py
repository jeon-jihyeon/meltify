import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKILLS = sorted((ROOT / "plugin" / "skills").glob("*/SKILL.md"))
# Only standard Agent Skills fields, so the same files load in other agents
ALLOWED = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}


def _frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text("utf-8")
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    assert m, f"{path} has no frontmatter"
    keys = {}
    for line in m.group(1).splitlines():
        if line and not line.startswith(" "):
            k, _, v = line.partition(":")
            keys[k] = v.strip()
    return keys


def test_versions_are_in_sync():
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "sync_version.py"), "--check"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    manifest = json.loads((ROOT / "plugin/.claude-plugin/plugin.json").read_text())
    assert manifest["version"] == version


def test_marketplace_points_at_plugin():
    market = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
    assert market["plugins"][0]["name"] == "meltify"
    assert (ROOT / market["plugins"][0]["source"] / ".claude-plugin/plugin.json").is_file()


def test_there_are_skills():
    assert SKILLS


@pytest.mark.parametrize("skill", SKILLS, ids=lambda p: p.parent.name)
def test_skill_frontmatter(skill):
    fm = _frontmatter(skill)
    assert set(fm) <= ALLOWED
    assert fm["name"] == skill.parent.name
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", fm["name"]) and len(fm["name"]) <= 64
    assert fm["name"].startswith("meltify-")
    assert 0 < len(fm["description"]) <= 1024
    assert len(skill.read_text().splitlines()) <= 500


@pytest.mark.parametrize("skill", SKILLS, ids=lambda p: p.parent.name)
def test_skill_launcher_is_an_exact_copy(skill):
    template = (ROOT / "scripts/launcher.sh").read_text()
    run = skill.parent / "scripts" / "run"
    assert run.read_text() == template
    assert run.stat().st_mode & 0o111


@pytest.mark.parametrize("skill", SKILLS, ids=lambda p: p.parent.name)
def test_skill_bash_grant_is_limited_to_meltify(skill):
    grants = re.findall(r"Bash(?:\(([^)]*)\))?", _frontmatter(skill)["allowed-tools"])
    assert grants and all(
        g.startswith(("meltify ", "${CLAUDE_SKILL_DIR}/scripts/run ")) for g in grants
    )


def test_help_lists_exit_codes():
    from meltify.cli import COMMANDS, _modules, build_parser

    parser = build_parser(_modules())
    assert "exit codes:" in parser.format_help()
    sub = next(a for a in parser._actions if a.dest == "command")
    for name in COMMANDS:
        assert "3  a needed engine" in sub.choices[name].format_help()
