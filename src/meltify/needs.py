"""Wording for needs lines, so every converter reports the same gap the same way"""

from __future__ import annotations

# What to install when only a LibreOffice render can read something
LIBREOFFICE = "install LibreOffice, or run meltify doctor --install libreoffice"


def count(n: int, noun: str) -> str:
    """Like `1 image` or `1,200 images`"""
    return f"{n:,} {noun}{'s' if n > 1 else ''}"


def not_read(n: int, what: str, why: str = "") -> str:
    """Like `2 images not read (why)`"""
    return f"{count(n, what)} not read" + (f" ({why})" if why else "")


def error_note(e: BaseException, limit: int = 200) -> str:
    return f"{type(e).__name__}: {e}"[:limit]
