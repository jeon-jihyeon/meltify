from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

T = TypeVar("T")


class MissingTool(Exception):
    """A binary, package or key the command needs isn't available"""

    def __init__(self, name: str, hint: str) -> None:
        super().__init__(f"{name} not available")
        self.name = name
        self.hint = hint


@dataclass
class Outcome:
    value: Any = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def attempt(fn: Callable[..., T], *args: Any, **kwargs: Any) -> Outcome:
    # One bad file or engine shouldn't stop a batch, so keep the error as data
    try:
        return Outcome(value=fn(*args, **kwargs))
    except MissingTool:
        raise
    except Exception as e:  # noqa: BLE001
        return Outcome(error=f"{type(e).__name__}: {e}"[:300])


def require_binary(name: str, hint: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise MissingTool(name, hint)
    return path


def run(
    args: Sequence[str], *, timeout: float | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        # ffmpeg and friends report the cause in the last lines of stderr
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
        raise RuntimeError(f"{args[0]} exited {proc.returncode}: {' | '.join(tail)}")
    return proc
