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


# Longest error note for each place it shows up. A needs line keeps it short, a row,
# warning or log line holds one item's failure, a command's own error gets more room,
# and a doctor table cell less
NEED_NOTE = 200
ITEM_NOTE = 300
COMMAND_NOTE = 500
CELL_NOTE = 120


def error_note(e: BaseException, limit: int = NEED_NOTE) -> str:
    return f"{type(e).__name__}: {e}"[:limit]
