"""Pull frames and audio out of a media file with the ffmpeg binary"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from meltify.safe import require_binary, run

HINT = "brew install ffmpeg or apt install ffmpeg"
PTS = re.compile(r"pts_time:\s*([0-9.]+)")
# Seconds a probe of an unknown file gets, since it may not be media at all
PROBE_TIMEOUT = 20


@dataclass(frozen=True)
class Window:
    start: float | None = None
    end: float | None = None

    def input_args(self) -> list[str]:
        # -ss before -i seeks fast, but timestamps then restart at zero,
        # so offset adds the start back
        args = []
        if self.start is not None:
            args += ["-ss", f"{self.start}"]
        if self.end is not None:
            args += ["-to", f"{self.end}"]
        return args

    def offset(self) -> float:
        return self.start or 0.0


def _arg(path: Path) -> str:
    # An absolute path never starts with a dash, so a file named like an option stays a file
    return str(path.resolve())


def _has_stream(path: Path, kind: str) -> bool:
    probe = require_binary("ffprobe", HINT)
    out = run(
        [
            probe, "-v", "error", "-select_streams", kind, "-show_entries", "stream=index",
            "-of", "csv=p=0", "-i", _arg(path),
        ]
    )  # fmt: skip
    return bool(out.stdout.strip())


def has_video(path: Path) -> bool:
    return _has_stream(path, "v")


def has_audio(path: Path) -> bool:
    return _has_stream(path, "a")


def media_kind(path: Path) -> str | None:
    """`video` or `audio` when ffprobe finds a timed stream, None when it can't tell

    ffprobe opens single pictures too, through its image demuxers, and ffmpeg may still have
    no decoder for them, so those don't count however short a duration they report
    """
    probe = shutil.which("ffprobe")
    if probe is None:
        return None
    try:
        proc = run(
            [probe, "-v", "error", "-show_entries", "format=duration,format_name:stream=codec_type",
             "-of", "json", "-i", _arg(path)],
            timeout=PROBE_TIMEOUT, check=False,
        )  # fmt: skip
        info = json.loads(proc.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    found = info.get("format", {})
    demuxer = found.get("format_name") or ""
    if proc.returncode != 0 or demuxer == "image2" or demuxer.endswith("_pipe"):
        return None
    if not float(found.get("duration") or 0):
        return None
    types = {s.get("codec_type") for s in info.get("streams", [])}
    return "video" if "video" in types else "audio" if "audio" in types else None


def scene_frames(
    path: Path, out_dir: Path, threshold: float, window: Window
) -> list[tuple[Path, float]]:
    """The first frame and every frame whose scene score passes the threshold"""
    ff = require_binary("ffmpeg", HINT)
    out_dir.mkdir(parents=True, exist_ok=True)
    proc = run(
        [
            ff, "-hide_banner", "-y", *window.input_args(), "-i", _arg(path),
            "-vf", f"select='eq(n\\,0)+gt(scene\\,{threshold})',showinfo",
            "-fps_mode", "vfr", "-q:v", "2", _arg(out_dir / "s_%05d.jpg"),
        ]
    )  # fmt: skip
    times = [float(m) for m in PTS.findall(proc.stderr)]
    files = sorted(out_dir.glob("s_*.jpg"))
    return [(f, round(t + window.offset(), 2)) for f, t in zip(files, times, strict=False)]


def interval_frames(
    path: Path, out_dir: Path, fps: float, window: Window
) -> list[tuple[Path, float]]:
    ff = require_binary("ffmpeg", HINT)
    out_dir.mkdir(parents=True, exist_ok=True)
    run(
        [
            ff, "-hide_banner", "-loglevel", "error", "-y", *window.input_args(), "-i", _arg(path),
            "-vf", f"fps={fps}", "-q:v", "3", _arg(out_dir / "t_%05d.jpg"),
        ]
    )  # fmt: skip
    # The fps filter emits frame k at roughly k / fps seconds from the window start
    return [
        (f, round(window.offset() + i / fps, 2))
        for i, f in enumerate(sorted(out_dir.glob("t_*.jpg")))
    ]


def audio(path: Path, target: Path, window: Window) -> Path:
    ff = require_binary("ffmpeg", HINT)
    target.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            ff, "-hide_banner", "-loglevel", "error", "-y", *window.input_args(), "-i", _arg(path),
            "-vn", "-ac", "1", "-ar", "16000", _arg(target),
        ]
    )  # fmt: skip
    return target
