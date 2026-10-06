"""Melt files of any format into prompt-ready text where every line cites its source

Only the names in `__all__` are public. Every other module is internal and can change
in any release
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING, Any

from meltify.safe import MissingTool

if TYPE_CHECKING:
    from meltify.evidence import Envelope

try:
    __version__ = version("meltify")
except PackageNotFoundError:
    __version__ = "0.0.0"

__all__ = ["Envelope", "MissingTool", "__version__", "read"]


def read(
    *paths: str | Path,
    out: str | Path | None = None,
    config_file: str | Path | None = None,
    password: str | None = None,
    **options: Any,
) -> Envelope:
    """Melt files, folders and URLs the way `meltify read` does

    `options` takes the command's flags as keywords, like `shallow=True` or `budget=60`.
    Each result row cites its source and points at the markdown it wrote under `out`.
    Raises MissingTool when a needed engine or extra isn't installed
    """
    import argparse

    from meltify import config
    from meltify.commands import read as command

    if not paths:
        raise TypeError("read() needs at least one path")
    parser = argparse.ArgumentParser()
    actions = command.add_arguments(parser)
    args = parser.parse_args(["-"])
    args.paths = [str(p) for p in paths]
    for name, value in options.items():
        # password stays a keyword of its own, so it never looks like a command line flag
        if name in ("paths", "password") or name not in actions:
            raise TypeError(f"read() got an unexpected keyword argument {name!r}")
        convert = actions[name].type
        # A string gets the flag's own type, as argparse would give it, so
        # password_file="pw.txt" arrives as the Path the command reads
        setattr(args, name, convert(value) if isinstance(value, str) and convert else value)
    overrides = {"out_dir": str(out)} if out is not None else None
    settings = config.load(overrides, project=Path(config_file) if config_file else None)
    return command.run(args, settings, password)


def __getattr__(name: str) -> Any:
    # Envelope loads lazily, so `meltify --help` doesn't pay for the import
    if name == "Envelope":
        from meltify.evidence import Envelope

        return Envelope
    raise AttributeError(f"module 'meltify' has no attribute {name!r}")
