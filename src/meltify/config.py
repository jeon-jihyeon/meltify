"""Layered settings, from lowest to highest precedence

1. Packaged defaults.toml
2. User config at $XDG_CONFIG_HOME/meltify/config.toml
3. Project meltify.toml, searched from cwd up to the git root, or an explicit --config path.
   Outside a git repo, only cwd is searched
4. Environment variables MELTIFY_<KEY> and MELTIFY_<SECTION>_<KEY>
5. Command-line overrides such as --out
"""

from __future__ import annotations

import copy
import os
import tomllib
from collections.abc import Mapping
from importlib.resources import files
from pathlib import Path
from typing import Any, TypeVar

from meltify import paths

T = TypeVar("T")

PROJECT_FILE = "meltify.toml"
ENV_PREFIX = "MELTIFY_"


class ConfigError(Exception):
    pass


def defaults() -> dict[str, Any]:
    return tomllib.loads(files("meltify.data").joinpath("defaults.toml").read_text("utf-8"))


def find_project_file(start: Path) -> Path | None:
    chain = [start, *start.parents]
    root = next((i for i, d in enumerate(chain) if (d / ".git").exists()), None)
    # Stop at the repo boundary, so a parent project never leaks in.
    # Without a repo, only cwd counts, so a stray file in HOME is never read
    for d in chain[: 1 if root is None else root + 1]:
        if (candidate := d / PROJECT_FILE).is_file():
            return candidate
    return None


def _read(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text("utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e


def _merge(base: dict[str, Any], top: Mapping[str, Any]) -> None:
    for k, v in top.items():
        # None means the caller didn't set it, so the lower layer wins
        if v is None:
            continue
        if isinstance(v, Mapping) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = copy.deepcopy(v)


def _unknown(layer: Mapping[str, Any], base: Mapping[str, Any], path: Path) -> list[str]:
    """A warning per key in `layer` that no default names, since a typo would be ignored"""
    found = []
    for key, value in layer.items():
        known = base.get(key)
        if known is None:
            found.append(key)
        elif isinstance(known, dict) and isinstance(value, Mapping):
            found += [f"{key}.{field}" for field in value if field not in known]
    return [f"{path}: unknown setting {name}, check the spelling" for name in found]


def _coerce(name: str, raw: str, like: Any) -> Any:
    # The packaged default decides the type, since env values are always plain strings
    if isinstance(like, dict):
        raise ConfigError(f"{name}: tables like this one can only be set in a config file")
    if isinstance(like, bool):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    try:
        if isinstance(like, int):
            return int(raw)
        if isinstance(like, float):
            return float(raw)
    except ValueError as e:
        raise ConfigError(f"{name}={raw!r} is not a valid {type(like).__name__}") from e
    if isinstance(like, list):
        return [s.strip() for s in raw.split(",") if s.strip()]
    return raw


def _from_env(base: dict[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, raw in env.items():
        if not name.startswith(ENV_PREFIX):
            continue
        key = name[len(ENV_PREFIX) :].lower()
        if key in base and not isinstance(base[key], dict):
            out[key] = _coerce(name, raw, base[key])
            continue
        for section, values in base.items():
            if not isinstance(values, dict) or not key.startswith(section + "_"):
                continue
            field = key[len(section) + 1 :]
            if (like := values.get(field)) is not None:
                out.setdefault(section, {})[field] = _coerce(name, raw, like)
    return out


def pick(flag: T | None, setting: T) -> T:
    """The flag when given, else the merged setting

    Only a flag left out falls back, so `--scene 0` means 0 instead of the default
    """
    return setting if flag is None else flag


def overrides(*, out: str | Path | None, lang: str | None) -> dict[str, Any]:
    """The command-line layer, from flags that map onto settings"""
    layer = {"out_dir": None if out is None else str(out), "lang": lang}
    return {k: v for k, v in layer.items() if v is not None}


def load(
    overrides: Mapping[str, Any] | None = None,
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    project: Path | None = None,
) -> dict[str, Any]:
    env = os.environ if env is None else env
    cwd = Path.cwd() if cwd is None else cwd
    known = defaults()
    settings = copy.deepcopy(known)
    sources = ["defaults"]

    layers: list[Path] = []
    if (u := paths.user_config(env)).is_file():
        layers.append(u)
    if project is not None:
        if not project.is_file():
            raise ConfigError(f"config file not found: {project}")
        layers.append(project)
    elif (p := find_project_file(cwd)) is not None:
        layers.append(p)
    warnings: list[str] = []
    for path in layers:
        layer = _read(path)
        warnings += _unknown(layer, known, path)
        _merge(settings, layer)
        sources.append(str(path))

    if env_layer := _from_env(known, env):
        _merge(settings, env_layer)
        sources.append("env")
    if overrides:
        _merge(settings, overrides)
        sources.append("cli")

    settings["_sources"] = sources
    settings["_warnings"] = warnings
    return settings
