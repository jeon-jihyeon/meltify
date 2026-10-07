"""Video URLs through yt-dlp, and the subtitles that come with a video"""

from __future__ import annotations

import glob
import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING

from meltify.converters.subtitle import parse_subtitles
from meltify.evidence import Src, span
from meltify.ffmpeg import Window
from meltify.safe import MissingTool

SUBTITLES = {".vtt", ".srt"}

if TYPE_CHECKING:
    from meltify.converters import Converted

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


def recording(kind: str, src: Src, clip: Path | None, subs: list[Path]) -> Converted:
    """The recording's transcript from `subs` when they have one, and a job for the rest

    Subtitles are exact and free, so speech recognition only runs without them. With
    --subs-only they're all there is, and no job is queued
    """
    from meltify.converters import Block, Converted, RecognizeJob
    from meltify.converters.run import current

    run = current()
    out = Converted("media")
    found = subtitles(subs, run.window)
    if found:
        out.blocks.append(Block(src, transcript(*found)))
    elif run.subs_only:
        out.needs.append("no subtitles")
    if clip is not None and not run.subs_only:
        out.jobs.append(RecognizeJob(kind, src, path=clip, listen=not found))
    return out


def transcript(sub: Path, cues: list[Cue]) -> str:
    lines = [f"{span(start, end)}| {text}" for start, end, text in cues]
    return "\n".join([f"> from subtitles {sub.name}", *lines])


def subtitles(files: list[Path], window: Window) -> tuple[Path, list[Cue]] | None:
    """The first file with cues inside the window, and those cues

    Subtitles are free and exact, so they stand in for speech recognition when present
    """
    for sub in files:
        cues = [
            (s, e, text)
            for s, e, text in parse_subtitles(sub.read_text("utf-8", errors="replace"))
            if (window.start is None or e >= window.start)
            and (window.end is None or s <= window.end)
        ]
        if cues:
            return sub, cues
    return None
