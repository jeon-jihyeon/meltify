"""Where meltify keeps its config, cache and downloads, following the XDG base directories"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


def _base(env: Mapping[str, str], name: str, fallback: Path) -> Path:
    return Path(env.get(name) or str(fallback))


def user_config(env: Mapping[str, str]) -> Path:
    return _base(env, "XDG_CONFIG_HOME", Path.home() / ".config") / "meltify" / "config.toml"


def cache_dir(env: Mapping[str, str] = os.environ) -> Path:
    return _base(env, "XDG_CACHE_HOME", Path.home() / ".cache") / "meltify"


def data_dir(env: Mapping[str, str] = os.environ) -> Path:
    # Must match the launcher, so it finds the venv installed here
    if env.get("CLAUDE_PLUGIN_DATA"):
        return Path(env["CLAUDE_PLUGIN_DATA"])
    return _base(env, "XDG_DATA_HOME", Path.home() / ".local" / "share") / "meltify"


def tools_dir(env: Mapping[str, str] = os.environ) -> Path:
    """Helper binaries `doctor --install` downloads, like 7-Zip and a portable LibreOffice"""
    return data_dir(env) / "tools"
