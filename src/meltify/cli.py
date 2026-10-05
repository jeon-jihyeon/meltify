from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

from meltify import __version__, config
from meltify.evidence import MISSING, USAGE, Envelope
from meltify.output import emit
from meltify.safe import MissingTool

# Each module exposes NAME, HELP, COLUMNS, add_arguments and run.
# Heavy imports live inside run, so --help and --version start fast
COMMANDS: list[str] = ["read", "ocr", "hidden", "media", "submit", "check", "brief", "doctor"]

USAGE_ERROR = 2

EXIT_CODES = """exit codes:
  0  success
  1  violations, failed checks or other errors
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
        description="Melt files of any format into prompt-ready text, with a citation per item",
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


def main(argv: Sequence[str] | None = None) -> int:
    modules = _modules()
    parser = build_parser(modules)
    args = parser.parse_args(argv)
    if not getattr(args, "_module", None):
        parser.print_help(sys.stderr)
        return USAGE_ERROR
    module = args._module

    overrides = {"out_dir": str(args.out)} if args.out is not None else None
    try:
        settings = config.load(overrides, project=args.config)
    except config.ConfigError as e:
        # Report it in the envelope, so --json callers still get valid JSON
        env = Envelope(command=module.NAME, version=__version__)
        env.error(USAGE, f"config: {e}")
        emit(env, as_json=args.json, columns=module.COLUMNS, limit=args.limit)
        return env.exit_code

    try:
        env = module.run(args, settings)
    except MissingTool as e:
        env = Envelope(command=module.NAME, version=__version__)
        env.error(MISSING, str(e), e.hint)
    except (FileNotFoundError, IsADirectoryError, ValueError) as e:
        # Report bad input in the envelope, so --json callers still get valid JSON
        env = Envelope(command=module.NAME, version=__version__)
        env.error(USAGE, f"{type(e).__name__}: {e}")
    emit(env, as_json=args.json, columns=env.columns or module.COLUMNS, limit=args.limit)
    return env.exit_code
