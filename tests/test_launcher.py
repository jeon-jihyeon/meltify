import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "scripts" / "launcher.sh"
VERSION = next(
    line.split('"')[1] for line in TEMPLATE.read_text().splitlines() if line.startswith("VERSION=")
)


def _exe(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


@pytest.fixture
def sandbox(tmp_path):
    # A launcher copy outside any source tree, with a PATH of only fakes and core tools
    home = tmp_path / "home"
    bindir = tmp_path / "fakebin"
    launcher_dir = tmp_path / "plugin" / "bin"
    for d in (home, bindir, launcher_dir):
        d.mkdir(parents=True)
    launcher = launcher_dir / "meltify"
    shutil.copy(TEMPLATE, launcher)
    launcher.chmod(0o755)
    for tool in ("sh", "dirname", "cat", "awk", "grep"):
        real = shutil.which(tool)
        assert real
        os.symlink(real, bindir / tool)
    env = {"HOME": str(home), "PATH": str(bindir)}
    return launcher, bindir, env, home


def _run(launcher, env, *args):
    return subprocess.run([str(launcher), *args], env=env, capture_output=True, text=True)


def test_prefers_same_version_on_path(sandbox):
    launcher, bindir, env, _ = sandbox
    _exe(
        bindir / "meltify",
        f'[ "$1" = --version ] && echo "meltify {VERSION}" && exit 0\necho path "$@"\n',
    )
    _exe(bindir / "uvx", 'echo uvx "$@"\n')
    assert _run(launcher, env, "doctor").stdout.strip() == "path doctor"


def test_other_path_version_falls_through_to_uvx(sandbox):
    launcher, bindir, env, _ = sandbox
    _exe(bindir / "meltify", '[ "$1" = --version ] && echo "meltify 0.0.1" && exit 0\necho path\n')
    _exe(bindir / "uvx", 'echo uvx "$@"\n')
    out = _run(launcher, env, "doctor").stdout.strip()
    assert out == f"uvx --quiet --from meltify=={VERSION} meltify doctor"
    env["MELTIFY_ALLOW_PATH"] = "1"
    assert _run(launcher, env, "doctor").stdout.strip() == "path"


def _venv(data: Path, version: str = VERSION) -> None:
    (data / "venv/bin").mkdir(parents=True)
    _exe(data / "venv/bin/meltify", 'echo venv "$@"\n')
    (data / "venv/.meltify-version").write_text(version)


def test_venv_from_doctor_install(sandbox):
    launcher, _, env, home = sandbox
    _venv(home / ".local/share/meltify")
    assert _run(launcher, env, "read", "a.png").stdout.strip() == "venv read a.png"


def test_venv_beats_same_version_on_path(sandbox):
    # doctor --install puts extras only into the venv, so it must win over a bare PATH install
    launcher, bindir, env, home = sandbox
    _venv(home / ".local/share/meltify")
    _exe(
        bindir / "meltify",
        f'[ "$1" = --version ] && echo "meltify {VERSION}" && exit 0\necho path "$@"\n',
    )
    assert _run(launcher, env, "doctor").stdout.strip() == "venv doctor"


def test_launcher_copy_on_path_is_not_queried(sandbox, tmp_path):
    launcher, bindir, env, _ = sandbox
    shutil.copy(TEMPLATE, bindir / "meltify")
    (bindir / "meltify").chmod(0o755)
    log = tmp_path / "uvx.log"
    _exe(bindir / "uvx", f'echo "$@" >> "{log}"\necho uvx "$@"\n')
    out = _run(launcher, env, "doctor").stdout.strip()
    assert out == f"uvx --quiet --from meltify=={VERSION} meltify doctor"
    assert log.read_text().count("\n") == 1


def test_plugin_data_dir_and_extras_and_git(sandbox, tmp_path):
    launcher, bindir, env, home = sandbox
    _venv(home / ".local/share/meltify")
    plugin_data = tmp_path / "plugin-data"
    _venv(plugin_data, "0.0.1")
    _exe(bindir / "uvx", 'echo uvx "$@"\n')
    # CLAUDE_PLUGIN_DATA replaces the default data dir, so the HOME venv is ignored
    env.update({"CLAUDE_PLUGIN_DATA": str(plugin_data)})
    env.update({"MELTIFY_EXTRAS": "media,asr-mlx", "MELTIFY_FROM_GIT": "1"})
    out = _run(launcher, env, "read", "x").stdout.strip()
    assert out == (
        f"uvx --quiet --from meltify[media,asr-mlx] @ "
        f"git+https://github.com/jeon-jihyeon/meltify@v{VERSION} meltify read x"
    )
    (plugin_data / "venv/.meltify-version").write_text(VERSION)
    assert _run(launcher, env, "read", "x").stdout.strip() == "venv read x"


def test_nothing_found_exits_one_with_hint(sandbox):
    launcher, _, env, _ = sandbox
    proc = _run(launcher, env, "doctor")
    assert proc.returncode == 1
    assert "Install uv" in proc.stderr


def test_source_checkout_runs_local_code(tmp_path):
    # The repo copy of the launcher sits inside the source tree, so it must pick uv run
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    for tool in ("sh", "dirname", "cat", "awk", "grep"):
        os.symlink(shutil.which(tool), bindir / tool)
    _exe(bindir / "uv", 'echo uv "$@"\n')
    env = {"HOME": str(tmp_path), "PATH": str(bindir)}
    out = _run(ROOT / "plugin/bin/meltify", env, "doctor").stdout.strip()
    assert out == f"uv run --quiet --project {ROOT} meltify doctor"
