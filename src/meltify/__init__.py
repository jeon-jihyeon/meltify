"""Melt files of any format into prompt-ready text where every line cites its source

Only the names in `__all__` are public. Every other module is internal and can change
in any release

`read` returns an `Envelope`, the same result `meltify read --json` prints through its
`to_dict()`:

- `results`: one dict per melted item, sorted by `cite`
- `errors`: what stopped the run, each a dict with `code`, `message` and maybe `hint`
- `warnings`: strings for what didn't stop it, like a failed item or a missing engine
- `inputs`, `artifacts` and `summary`: the paths given, files written such as
  `index.jsonl`, and the closing line
- `ok` is true when `errors` is empty, and `exit_code` is what the command would exit with

A `read` row holds `src` and `cite` for where the item came from, `kind`, `out` for the
markdown path, `chars`, `needs` for what's still unread and `hidden` for invisible PDF
spans. A failed item adds `error` and `hint`, an item a fallback read without a missing
extra adds just the `hint` for installing it, a URL adds `final_url`, `fetched_at`,
`sha256` and `etag`, and an OCR value the engines dispute gets its own row with `type`
set to `disputed`
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

    `options` takes the command's flags as keywords, like `shallow=True` or `budget=60`,
    and a string value gets the flag's own type. `out` replaces `--out` and `config_file`
    replaces `--config`. `password` outranks `password_file` and `MELTIFY_PASSWORD`.

    Each result row cites its source and points at the markdown it wrote under `out`.
    A file that needs a missing extra or tool doesn't raise: its row lists it in `needs`,
    with `error` and `hint` when nothing could be read. Raises MissingTool only when an
    OCR engine named in `engines` or a speech engine named in `asr` isn't available,
    FileNotFoundError for a path that doesn't exist, and TypeError for an unknown keyword
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
    overrides = config.overrides(out=out, lang=args.lang)
    settings = config.load(overrides, project=Path(config_file) if config_file else None)
    env = command.run(args, settings, password)
    env.warnings[:0] = settings["_warnings"]
    return env


def __getattr__(name: str) -> Any:
    # Envelope loads lazily, so `meltify --help` doesn't pay for the import
    if name == "Envelope":
        from meltify.evidence import Envelope

        return Envelope
    raise AttributeError(f"module 'meltify' has no attribute {name!r}")
