from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, TypeVar, overload

from meltify.needs import ITEM_NOTE, error_note

T = TypeVar("T")


class MissingTool(Exception):
    """A binary, package or key the command needs isn't available"""

    def __init__(self, name: str, hint: str) -> None:
        super().__init__(f"{name} not available")
        self.name = name
        self.hint = hint


class Unreachable(Exception):
    """An engine's server didn't answer, so every later call this run would wait and fail too"""


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
    except (MissingTool, Unreachable):
        raise
    except Exception as e:  # noqa: BLE001
        return Outcome(error=error_note(e, ITEM_NOTE))


def require_binary(name: str, hint: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise MissingTool(name, hint)
    return path


def kill_group(proc: subprocess.Popen) -> None:
    """Kill a process started in its own session, with everything it spawned"""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        proc.kill()


@overload
def run(
    args: Sequence[str],
    *,
    timeout: float | None = None,
    check: bool = True,
    input: str | None = None,
    text: Literal[True] = True,
) -> subprocess.CompletedProcess[str]: ...


@overload
def run(
    args: Sequence[str],
    *,
    timeout: float | None = None,
    check: bool = True,
    input: bytes | None = None,
    text: Literal[False],
) -> subprocess.CompletedProcess[bytes]: ...


def run(
    args: Sequence[str],
    *,
    timeout: float | None = None,
    check: bool = True,
    input: str | bytes | None = None,
    text: bool = True,
) -> subprocess.CompletedProcess[Any]:
    """Output of a finished program, which never outlives a timeout or an interrupt

    `input` goes in on stdin, the way to hand over a secret that argv would show to every
    user on the machine. Text output decodes with replacement, since tools print file names
    in whatever encoding they were stored in
    """
    # Its own session, so a timeout or an interrupt also kills the helpers it started, like
    # the soffice.bin that LibreOffice's launcher script leaves running on Linux
    with subprocess.Popen(
        list(args),
        stdin=subprocess.DEVNULL if input is None else subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
        errors="replace" if text else None,
        start_new_session=True,
    ) as proc:
        try:
            stdout, stderr = proc.communicate(input, timeout=timeout)
        except BaseException:
            kill_group(proc)
            proc.wait()
            raise
    if check and proc.returncode != 0:
        # ffmpeg and friends report the cause in the last lines of stderr
        said = stderr or stdout
        if isinstance(said, bytes):
            said = said.decode("utf-8", "replace")
        tail = said.strip().splitlines()[-3:]
        raise RuntimeError(f"{args[0]} exited {proc.returncode}: {' | '.join(tail)}")
    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)
