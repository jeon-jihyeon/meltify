"""Keep every version string and launcher copy in sync with pyproject.toml

Run it without arguments to write, or with --check in CI to fail on drift. It covers:
1. The version in plugin/.claude-plugin/plugin.json
2. VERSION in scripts/launcher.sh
3. plugin/bin/meltify and plugin/skills/*/scripts/run as exact copies of the launcher
4. metadata.version in every SKILL.md
"""

from __future__ import annotations

import json
import re
import stat
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "scripts" / "launcher.sh"
MANIFEST = ROOT / "plugin" / ".claude-plugin" / "plugin.json"
SKILLS = ROOT / "plugin" / "skills"


def version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]["version"]


def expected(v: str) -> dict[Path, str]:
    launcher = re.sub(
        r'^VERSION="[^"]*"', f'VERSION="{v}"', TEMPLATE.read_text("utf-8"), flags=re.M
    )
    out = {TEMPLATE: launcher, ROOT / "plugin" / "bin" / "meltify": launcher}
    for skill in sorted(SKILLS.glob("*/SKILL.md")):
        out[skill.parent / "scripts" / "run"] = launcher
        text = skill.read_text("utf-8")
        out[skill] = re.sub(r'(\n  version: )"[^"]*"', rf'\g<1>"{v}"', text, count=1)
    manifest = json.loads(MANIFEST.read_text("utf-8"))
    manifest["version"] = v
    out[MANIFEST] = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    return out


def main(argv: list[str]) -> int:
    check = "--check" in argv
    drift = []
    for path, text in expected(version()).items():
        current = path.read_text("utf-8") if path.exists() else None
        if current == text:
            continue
        drift.append(path.relative_to(ROOT))
        if not check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            if path.name in {"run", "meltify", "launcher.sh"}:
                path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    for p in drift:
        print(f"{'out of date' if check else 'wrote'}: {p}")
    return 1 if check and drift else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
