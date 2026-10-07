"""Melt files of any format into prompt-ready text where every block cites its source

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
spans. A failed item adds `error` and `hint`, an item a fallback read in place of a missing
extra adds just the `hint` for installing it, and a URL adds `final_url`, `fetched_at`,
`sha256` and `etag`

Rows of kind `disputed`, `hidden`, `contrast` and `frame` point at a place inside an item
instead: an OCR value the engines dispute, a hidden PDF span with its `text` and `reasons`,
a PDF page rendered in stretched contrast and a kept video frame, each picture at `path`
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
    FileNotFoundError for a path that doesn't exist, TypeError for an unknown keyword and
    ValueError for a value the flag would refuse
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
        setattr(args, name, _keyword(name, actions[name], value))
    overrides = config.overrides(out=out, lang=args.lang)
    settings = config.load(overrides, project=Path(config_file) if config_file else None)
    env = command.run(args, settings, password)
    env.warnings[:0] = settings["_warnings"]
    return env


def _keyword(name: str, action: Any, value: Any) -> Any:
    """`value` checked and typed the way the flag's own parsing would

    A plain value goes through the flag's type as its text, so password_file="pw.txt"
    arrives as the Path the command reads and pages=2 as the page range "2"
    """
    import argparse

    def one(v: Any) -> Any:
        if action.type is not None and isinstance(v, str | int | float) and not isinstance(v, bool):
            try:
                v = action.type(str(v))
            except argparse.ArgumentTypeError as e:
                raise ValueError(f"{name}: {e}") from None
        if action.choices is not None and v not in action.choices:
            raise ValueError(f"{name} must be one of {', '.join(map(str, action.choices))}")
        return v

    # A flag given more than once, like reading, takes a list, and one value stands for one
    if isinstance(action.default, list):
        return [one(v) for v in ([value] if isinstance(value, str) else value)]
    return one(value)


def __getattr__(name: str) -> Any:
    # Envelope loads lazily, so `meltify --help` doesn't pay for the import
    if name == "Envelope":
        from meltify.evidence import Envelope

        return Envelope
    raise AttributeError(f"module 'meltify' has no attribute {name!r}")
