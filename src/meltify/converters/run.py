"""What one read run decides for every converter, set once and seen by all its workers

Converters only get a path and a src, so the run's password, switches and shared state
live in one context variable. read sets it per run, and each worker thread runs in a copy
of that context, so concurrent runs in one process never see each other's
"""

from __future__ import annotations

import atexit
import shutil
import tempfile
import threading
from collections.abc import Iterable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from meltify.ffmpeg import Window
from meltify.files import Pages

# PyMuPDF isn't thread-safe, and read converts files in parallel. Reentrant, because a PDF
# conversion that holds it can reach a page render through an embedded picture
LOCK = threading.RLock()
# Every workdir not yet removed, so the exit cleanup reaches the ones a run never released
_LIVE: set[Path] = set()
_LIVE_LOCK = threading.Lock()
# Fewest live workdirs that start a sweep for the ones callers already removed
SWEEP_MIN = 64
_sweep_at = SWEEP_MIN


class Temps:
    """Workdirs one read run made, removed together once its OCR stage is done"""

    def __init__(self) -> None:
        self.folders: list[Path] = []
        self.lock = threading.Lock()

    def add(self, folder: Path) -> None:
        with self.lock:
            self.folders.append(folder)

    def remove(self) -> None:
        with self.lock:
            held, self.folders = self.folders, []
        for folder in held:
            shutil.rmtree(folder, ignore_errors=True)
        with _LIVE_LOCK:
            _LIVE.difference_update(held)


class Budget:
    """Decompressed bytes per input, shared by every archive nested under it in one run"""

    def __init__(self) -> None:
        self.spent: dict[str, int] = {}
        self.lock = threading.Lock()

    def add(self, key: str, n: int) -> int:
        """The input's new total"""
        with self.lock:
            self.spent[key] = self.spent.get(key, 0) + n
            return self.spent[key]


@dataclass
class WebKit:
    # Off for the rest of the run once WebKit hangs, which it does without a window server,
    # like over SSH, so later files don't each wait out the timeout
    working: bool = True


@dataclass(frozen=True)
class PdfLook:
    """How closely read looks at each PDF item, plain values so a PDF worker process gets them

    A PDF that a renderer drew for another format is read with the defaults instead, since
    these flags speak of the user's PDFs
    """

    # --pages, the pages to melt
    pages: Pages | None = None
    # --ocr-pages, which OCRs every page as drawn, text layer or not
    ocr_pages: bool = False
    # --hidden, which keeps a result row per hidden span instead of only counting them
    hidden: bool = False
    # --contrast renders each page at this dpi with stretched contrast, None for no renders
    contrast_dpi: int | None = None


@dataclass(frozen=True)
class RunContext:
    password: str | None = None
    fallback: bool = True  # read.fallback
    # read --shallow, which starts no renderer or subprocess that only feeds OCR
    shallow: bool = False
    # read.parquet_rows, 0 for every row. A data file can hold billions of rows, so only the
    # head is melted and the rest counted
    parquet_rows: int = 200
    quicklook: bool = True  # render.quicklook
    pdf: PdfLook = field(default_factory=PdfLook)
    # read --start and --end, the part of each recording to transcribe and look at
    window: Window = field(default_factory=Window)
    # read --subs-only, which takes a recording's transcript from its subtitles alone
    subs_only: bool = False
    # Shared by the run's workers but never by two runs, so equality leaves them out
    budget: Budget = field(default_factory=Budget, compare=False)
    webkit: WebKit = field(default_factory=WebKit, compare=False)
    # Where workdirs go to be removed with the run, None to leave them to the caller or exit
    temps: Temps | None = field(default=None, compare=False)

    @classmethod
    def from_settings(cls, settings: Mapping[str, Any], **run: Any) -> RunContext:
        """The switches `settings` holds, with per-run values like the password in `run`"""
        return cls(
            fallback=bool(settings["read"]["fallback"]),
            parquet_rows=int(settings["read"]["parquet_rows"]),
            quicklook=bool(settings["render"]["quicklook"]),
            **run,
        )


_RUN: ContextVar[RunContext | None] = ContextVar("meltify_run", default=None)


def use(context: RunContext | None) -> None:
    """Make `context` the run for this context and the worker copies taken from it

    None ends the run, so later calls start fresh again
    """
    _RUN.set(context)


def current() -> RunContext:
    # Outside a run every call starts fresh, so a budget or a WebKit hang never outlives
    # the call that saw it
    return _RUN.get() or RunContext()


def _gone(paths: Iterable[Path]) -> list[Path]:
    # Callers remove most workdirs themselves, so a long-lived process would otherwise keep
    # every path it ever made
    return [p for p in paths if not p.exists()]


def workdir(prefix: str) -> Path:
    """A temp folder for a render or copy that the OCR stage opens later

    Image-only pages are read after every file is converted, so no converter can tell when
    the file is done with. The folder goes when the run finishes, at process exit at the
    latest, or earlier once the caller removes it itself
    """
    global _sweep_at
    folder = Path(tempfile.mkdtemp(prefix=prefix))
    with _LIVE_LOCK:
        _LIVE.add(folder)
        # Sweeping on every call would cost n squared exists checks for n workdirs. Waiting
        # until the set doubles keeps each sweep proportional to the workdirs made since the
        # last one
        if len(_LIVE) >= _sweep_at:
            _LIVE.difference_update(_gone(_LIVE))
            _sweep_at = max(SWEEP_MIN, 2 * len(_LIVE))
    temps = current().temps
    if temps is not None:
        temps.add(folder)
    return folder


@atexit.register
def _cleanup() -> None:
    with _LIVE_LOCK:
        held = list(_LIVE)
        _LIVE.clear()
    for folder in held:
        shutil.rmtree(folder, ignore_errors=True)
