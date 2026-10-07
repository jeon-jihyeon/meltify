"""Video URLs through yt-dlp, and the subtitles that come with a video"""

from __future__ import annotations

import glob
import importlib.util
from pathlib import Path

from meltify.converters.subtitle import parse_subtitles
from meltify.safe import MissingTool

SUBTITLES = {".vtt", ".srt"}

Cue = tuple[float, float, str]


def download(
    url: str,
    out_dir: Path,
    sub_langs: list[str],
    subs_only: bool,
    max_height: int,
    allow_private: bool = False,
) -> tuple[Path | None, list[Path]]:
    from meltify.fetch import check_url

    # yt-dlp's generic extractor fetches any address, so the given URL gets read's address
    # check. yt-dlp's own redirects and segment requests happen inside it, out of reach
    check_url(url, allow_private)
    if importlib.util.find_spec("yt_dlp") is None:
        raise MissingTool("yt-dlp", "meltify doctor --install media")
    import yt_dlp

    out_dir.mkdir(parents=True, exist_ok=True)
    opts = {
        "outtmpl": str(out_dir / "%(id)s.%(ext)s"),
        "format": f"bv*[height<={max_height}]+ba/b",
        "merge_output_format": "mp4",
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": sub_langs,
        "subtitlesformat": "vtt",
        "skip_download": subs_only,
        "quiet": True,
        "no_warnings": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    vid = info["id"]
    video = None if subs_only else next(iter(sorted(out_dir.glob(f"{vid}.mp4"))), None)
    return video, sorted(out_dir.glob(f"{vid}*.vtt"))


def sidecars(path: Path) -> list[Path]:
    # Require a dot after the stem, so clip2.vtt isn't mistaken for clip.mp4
    return sorted(
        p for p in path.parent.glob(f"{glob.escape(path.stem)}.*") if p.suffix.lower() in SUBTITLES
    )


def subtitles(
    files: list[Path], start: float | None = None, end: float | None = None
) -> tuple[Path, list[Cue]] | None:
    """The first file with cues inside the window, and those cues

    Subtitles are free and exact, so they stand in for speech recognition when present
    """
    for sub in files:
        cues = [
            (s, e, text)
            for s, e, text in parse_subtitles(sub.read_text("utf-8", errors="replace"))
            if (start is None or e >= start) and (end is None or s <= end)
        ]
        if cues:
            return sub, cues
    return None
