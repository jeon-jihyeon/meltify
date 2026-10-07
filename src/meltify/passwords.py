"""Passwords for encrypted inputs, and the needs lines for inputs that stay shut

The password stays out of argv, logs and cites
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Callable

from meltify.converters import run

LOCKED = "encrypted, set MELTIFY_PASSWORD or --password-file"
WRONG = "wrong password"
NEEDS_CRYPTO = "encrypted, needs the crypto extra (meltify doctor --install crypto)"
NEEDS_DRM = "DRM-protected, open it with the DRM client"
DISTRIBUTION = "encrypted for distribution, can't decrypt"


class Locked(Exception):
    """An encrypted input that can't be opened, its message the needs line to show"""


class WrongPassword(Exception):
    """A password that doesn't open this member, though it may still open the others"""


def cant_decrypt(why: str) -> str:
    return f"encrypted, can't decrypt ({why})"


def password() -> str | None:
    """The read run's password, else the one the shell carries"""
    return run.current().password or os.environ.get("MELTIFY_PASSWORD") or None


def given_password(
    args: argparse.Namespace, warn: Callable[[str], None], secret: str | None = None
) -> str | None:
    # A password handed over in Python never touched argv, so it skips the warning below
    if secret:
        return secret
    if args.password:
        # Anyone on the machine can read argv from the process list
        warn("--password is visible to other users, prefer --password-file or MELTIFY_PASSWORD")
    if args.password_file:
        # utf-8-sig drops the byte order mark Windows Notepad writes
        first = next(iter(args.password_file.read_text("utf-8-sig").splitlines()), "")
        if not first:
            raise ValueError(f"no password on the first line of {args.password_file}")
        return first
    # A password given for this run outranks the one the shell carries for every run
    return args.password or os.environ.get("MELTIFY_PASSWORD")
