"""Short synthetic media with known scene changes and captions"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

VTT = """WEBVTT

00:00:00.500 --> 00:00:01.800
<c>first words</c>

00:00:01.800 --> 00:00:03.000
first words more words

00:00:04.200 --> 00:00:05.500
blue part
"""


def scenes_video(path: Path) -> Path:
    """Three still scenes of clearly different brightness, two seconds each, with a tone"""
    ff = shutil.which("ffmpeg")
    assert ff
    # Brightness must differ, since the scene score reads luma and red and green are equal there
    colors = ["black", "white", "red"]
    args = [ff, "-hide_banner", "-loglevel", "error", "-y"]
    for c in colors:
        args += ["-f", "lavfi", "-i", f"color=c={c}:s=160x120:r=10:d=2"]
    args += ["-f", "lavfi", "-i", "sine=frequency=440:duration=6"]
    args += [
        "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
        "-map", "[v]", "-map", "3:a", "-shortest", "-pix_fmt", "yuv420p", str(path.resolve()),
    ]  # fmt: skip
    subprocess.run(args, check=True)
    return path
