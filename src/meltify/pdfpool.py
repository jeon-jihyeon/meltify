"""Worker processes that convert PDFs in parallel, since PyMuPDF is only unsafe across threads"""

from __future__ import annotations

import multiprocessing
import os
import pickle
import sys
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from contextlib import contextmanager
from pathlib import Path

from meltify.converters import Converted
from meltify.evidence import Src
from meltify.melt import FATAL


def _melt_pdf(path: Path, src: Src) -> Converted:
    """pdf's convert in a worker process, failing with an error the parent can unpickle"""
    from meltify.converters import pdf

    try:
        return pdf.convert(path, src)
    except FATAL:
        raise
    except BaseException as e:  # noqa: BLE001
        try:
            pickle.loads(pickle.dumps(e))
        except Exception:  # noqa: BLE001
            raise RuntimeError(f"{type(e).__name__}: {e}") from None
        raise


def _spawn_safe() -> bool:
    """Whether a spawned process can start without running the caller's script again

    Spawn imports the main module of the parent in every child. multiprocessing skips a
    module run with -m, and there's nothing to import from a REPL or `python -c`. The
    meltify console script only calls main under a `__name__` guard. Any other script
    may do its work at import time, so PDFs then stay in this process
    """
    main = sys.modules.get("__main__")
    name = getattr(getattr(main, "__spec__", None), "name", None) or ""
    if name == "__main__" or name.endswith(".__main__"):
        return True
    if getattr(main, "__file__", None) is None:
        return True
    from meltify import cli

    return getattr(main, "main", None) is cli.main


class PdfPool:
    """Worker processes for PDFs, since PyMuPDF is only unsafe across threads

    Each process holds its own MuPDF, so PDFs convert in parallel instead of one at a time
    under the shared lock. pdf's convert reads nothing but the path and src, as unlock
    already swapped an encrypted file for its decrypted copy. A pool that can't start or
    breaks hands its PDFs back to the lock
    """

    def __init__(self, workers: int) -> None:
        self.workers = workers
        self.executor: ProcessPoolExecutor | None = None
        self.broken = False
        self.lock = threading.Lock()
        # The run's first PDF converts under the lock in this process, so a run with one PDF
        # never pays about 200 ms to spawn a worker and import PyMuPDF again
        self.first_taken = False

    @classmethod
    @contextmanager
    def start(cls, jobs: int) -> Iterator[PdfPool | None]:
        if jobs <= 1 or not _spawn_safe():
            yield None
            return
        pool = cls(min(jobs, os.cpu_count() or 1))
        try:
            yield pool
        finally:
            pool.close()

    def convert(self, convert: Callable[..., Converted], path: Path, src: Src) -> Converted | None:
        """The converted PDF, or None when this file has to go through the lock"""
        from meltify.converters import pdf

        # A converter a test or plugin swapped in may not pickle
        if convert is not pdf.convert or self._take_first():
            return None
        if (executor := self._executor()) is None:
            return None
        try:
            task = executor.submit(_melt_pdf, path, src)
        except (BrokenProcessPool, RuntimeError):
            # A broken or closed pool refuses work
            self.broken = True
            return None
        try:
            return task.result()
        except BrokenProcessPool:
            # A worker that crashed takes the whole pool down
            self.broken = True
            return None

    def _take_first(self) -> bool:
        with self.lock:
            first, self.first_taken = not self.first_taken, True
            return first

    def _executor(self) -> ProcessPoolExecutor | None:
        with self.lock:
            if self.executor is None and not self.broken:
                try:
                    self.executor = ProcessPoolExecutor(
                        self.workers, mp_context=multiprocessing.get_context("spawn")
                    )
                except (OSError, ValueError, NotImplementedError):
                    self.broken = True
            return None if self.broken else self.executor

    def close(self) -> None:
        if self.executor is not None:
            self.executor.shutdown(wait=True, cancel_futures=True)
