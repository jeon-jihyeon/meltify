"""The password for encrypted inputs, kept out of argv, logs and cites, and the needs
lines for an input that stays shut
"""

from __future__ import annotations

import os

from meltify.converters import run

LOCKED = "encrypted, set MELTIFY_PASSWORD or --password-file"
WRONG = "wrong password"
NEEDS_CRYPTO = "encrypted, needs the crypto extra (meltify doctor --install crypto)"
NEEDS_DRM = "DRM-protected, open it with the DRM client"
DISTRIBUTION = "encrypted for distribution, can't decrypt"


class Locked(Exception):
    """An encrypted input that can't be opened, its message the needs line to show"""


class WrongPassword(Exception):
    """A password that doesn't open one member, while the rest of its container may still"""


def cant_decrypt(why: str) -> str:
    return f"encrypted, can't decrypt ({why})"


def password() -> str | None:
    """The read run's password, else the one the shell carries"""
    return run.current().password or os.environ.get("MELTIFY_PASSWORD") or None
