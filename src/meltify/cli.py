from __future__ import annotations

import argparse
import importlib
import os
import sys
import traceback
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

from meltify import __version__, config
from meltify.evidence import FAILED, MISSING, USAGE, Envelope
from meltify.needs import COMMAND_NOTE, error_note
from meltify.output import emit
from meltify.safe import MissingTool

# Each module exposes NAME, HELP, COLUMNS, add_arguments and run.
# Heavy imports live inside run, so --help and --version start fast
COMMANDS: list[str] = ["read", "doctor"]
# Commands folded into read in 0.3.0, with the flags that keep their meaning. They're removed
# in 0.4.0
DEPRECATED: dict[str, list[str]] = {
    "ocr": ["--ocr-pages"],
    "hidden": ["--hidden"],
    # media took interval frames at 1 fps by default, which read leaves off
    "media": ["--fps", "1"],
}

USAGE_ERROR = 2

EXIT_CODES = """exit codes:
  0  success
  1  a failure inside the command
  2  usage error or bad input
  3  a needed engine, key, binary or base module is missing"""


def _modules() -> list[ModuleType]:
    return [importlib.import_module(f"meltify.commands.{name}") for name in COMMANDS]


def _limit(raw: str) -> int:
    n = int(raw)
    if n < 0:
        raise argparse.ArgumentTypeError(f"must be 0 or more, got {n}")
    return n


def build_parser(modules: Sequence[ModuleType]) -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="print the full result envelope")
    common.add_argument("--out", type=Path, help="output directory for artifacts")
    common.add_argument("--config", type=Path, help="project config file instead of meltify.toml")
    common.add_argument(
        "--limit", type=_limit, default=50, help="rows shown in the table, 0 for all"
    )

    parser = argparse.ArgumentParser(
        prog="meltify",
        description=(
            "Melt files of any format into prompt-ready text where every block cites its source"
        ),
        epilog=EXIT_CODES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"meltify {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    for m in modules:
        p = sub.add_parser(
            m.NAME,
            help=m.HELP,
            description=m.HELP,
            epilog=EXIT_CODES,
            formatter_class=argparse.RawDescriptionHelpFormatter,
            parents=[common],
        )
        m.add_arguments(p)
        p.set_defaults(_module=m)
    return parser


def _failure(e: Exception) -> tuple[str, str, str | None]:
    """Error code, message and hint for an exception a command let through"""
    if isinstance(e, MissingTool):
        return MISSING, str(e), e.hint
    if isinstance(e, FileNotFoundError):
        return USAGE, f"no such file: {e.filename or e}", None
    if isinstance(e, IsADirectoryError):
        return USAGE, f"expected a file, got a folder: {e.filename or e}", None
    if isinstance(e, ValueError):
        # meltify raises plain ValueErrors whose message says what to fix. A subclass, like a
        # JSON or decode error from a library, keeps its name, since that's the only context
        return USAGE, str(e) if type(e) is ValueError else error_note(e, COMMAND_NOTE), None
    if os.environ.get("MELTIFY_DEBUG"):
        traceback.print_exc()
    return FAILED, error_note(e, COMMAND_NOTE), "set MELTIFY_DEBUG=1 to see the traceback"


def _undeprecated(argv: list[str]) -> tuple[list[str], str | None]:
    """The read command line an old command stands for, and the warning that says so"""
    if not argv or argv[0] not in DEPRECATED:
        return argv, None
    old, extra = argv[0], DEPRECATED[argv[0]]
    # media --no-frames meant no frames at all, which read spells --frames 0
    rest = [a for x in argv[1:] for a in (["--frames", "0"] if x == "--no-frames" else [x])]
    new = ["read", *extra, *rest]
    hint = " ".join(["meltify read", *extra])
    return new, f"meltify {old} is deprecated and will be removed in 0.4.0, use {hint} instead"


def main(argv: Sequence[str] | None = None) -> int:
    modules = _modules()
    parser = build_parser(modules)
    argv, deprecated = _undeprecated(list(sys.argv[1:] if argv is None else argv))
    args = parser.parse_args(argv)
    if not getattr(args, "_module", None):
        parser.print_help(sys.stderr)
        return USAGE_ERROR
    module = args._module

    overrides = config.overrides(out=args.out, lang=getattr(args, "lang", None))
    # Every failure lands in the envelope, so --json callers still get valid JSON
    try:
        settings = config.load(overrides, project=args.config)
    except config.ConfigError as e:
        env = Envelope(command=module.NAME, version=__version__)
        env.error(USAGE, f"config: {e}")
        emit(env, as_json=args.json, columns=module.COLUMNS, limit=args.limit)
        return env.exit_code

    try:
        env = module.run(args, settings)
    except Exception as e:  # noqa: BLE001
        env = Envelope(command=module.NAME, version=__version__)
        env.error(*_failure(e))
    env.warnings[:0] = settings["_warnings"]
    if deprecated:
        env.warnings.insert(0, deprecated)
    emit(env, as_json=args.json, columns=env.columns or module.COLUMNS, limit=args.limit)
    return env.exit_code
