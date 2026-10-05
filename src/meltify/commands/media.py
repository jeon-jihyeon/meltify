from __future__ import annotations

import argparse
import glob
import hashlib
import importlib.util
import re
import shutil
from pathlib import Path
from typing import Any

from meltify import __version__
from meltify.evidence import Envelope, Src, finding
from meltify.files import flat_name
from meltify.safe import MissingTool

NAME = "media"
HELP = "turn a video, recording or video URL into timestamped frames and a timestamped transcript"
COLUMNS = ["type", "text", "path", "cite"]

CUE = re.compile(r"(\d+:)?(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(\d+:)?(\d{2}):(\d{2})[.,](\d{3})")


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("source", help="local media file or URL")
    p.add_argument("--start", type=float, help="seconds from the start")
    p.add_argument("--end", type=float, help="seconds from the start")
    p.add_argument("--fps", type=float, help="interval frames per second")
    p.add_argument("--scene", type=float, help="scene-change threshold, 0 to 1")
    p.add_argument("--no-frames", action="store_true", help="transcript only")
    p.add_argument("--subs-only", action="store_true", help="use subtitles and skip ASR and frames")
    p.add_argument("--asr", help="mlx, whispercpp or api (default: asr.engine)")
    p.add_argument("--keep-duplicates", action="store_true", help="list near-identical frames too")


def _seconds(h: str | None, m: str, s: str, ms: str) -> float:
    return int((h or "0:")[:-1]) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def parse_subtitles(text: str) -> list[tuple[float, float, str]]:
    """VTT or SRT cues with tags stripped and rolling repeats merged"""
    cues: list[tuple[float, float, str]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = CUE.search(lines[i])
        if not m:
            i += 1
            continue
        start = _seconds(m.group(1), m.group(2), m.group(3), m.group(4))
        end = _seconds(m.group(5), m.group(6), m.group(7), m.group(8))
        body = []
        i += 1
        while i < len(lines) and lines[i].strip():
            body.append(re.sub(r"<[^>]+>", "", lines[i]).strip())
            i += 1
        said = " ".join(b for b in body if b)
        # Auto captions repeat the previous line as they roll, so keep only the new text
        if cues and said.startswith(cues[-1][2]) and said != cues[-1][2]:
            said = said[len(cues[-1][2]) :].strip()
        if said and (not cues or said != cues[-1][2]):
            cues.append((round(start, 2), round(end, 2), said))
    return cues


def _download(
    url: str, out_dir: Path, sub_langs: list[str], subs_only: bool, max_height: int
) -> tuple[Path | None, list[Path]]:
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


def _sidecars(path: Path) -> list[Path]:
    # Require a dot after the stem, so clip2.vtt isn't mistaken for clip.mp4
    return sorted(
        p
        for p in path.parent.glob(f"{glob.escape(path.stem)}.*")
        if p.suffix.lower() in {".vtt", ".srt"}
    )


def _work_name(url: str) -> str:
    # The tail keeps the name readable, and the hash keeps URLs with the same tail apart
    tail = re.sub(r"[^\w.\-]+", "_", url)[-71:]
    return f"{tail}_{hashlib.sha256(url.encode()).hexdigest()[:8]}"


def run(args: argparse.Namespace, settings: dict[str, Any]) -> Envelope:
    from meltify import ffmpeg, imaging
    from meltify.engines import asr

    env = Envelope(command=NAME, version=__version__)
    conf = settings.get("media", {})
    lang = settings.get("lang", "ko")
    window = ffmpeg.Window(args.start, args.end)
    is_url = args.source.startswith(("http://", "https://"))
    base = Path(settings["out_dir"]) / "media"

    if is_url:
        work = base / _work_name(args.source)
        video, subs = _download(
            args.source,
            work,
            list(conf.get("sub_langs", ["ko", "en"])),
            args.subs_only,
            int(conf.get("max_height", 1080)),
        )
    else:
        video = Path(args.source)
        if not video.is_file():
            raise FileNotFoundError(video)
        work = base / flat_name(video)
        subs = _sidecars(video)
    env.inputs.append({"path": args.source})

    # Try subtitles first, since they're free and exact when present
    speech: list[tuple[float, float, str, str]] = []
    for sub in subs:
        for start, end, text in parse_subtitles(sub.read_text("utf-8", errors="replace")):
            if (args.start is None or end >= args.start) and (
                args.end is None or start <= args.end
            ):
                speech.append((start, end, text, f"subtitle {sub.name}"))
        if speech:
            break
    if not speech and not args.subs_only and video is not None:
        try:
            engine = asr.select(args.asr or settings.get("asr", {}).get("engine", "auto"), settings)
            wav = ffmpeg.audio(video, work / "audio.wav", window)
            for s in engine.transcribe(wav, lang):
                speech.append(
                    (
                        s.start + window.offset(),
                        s.end + window.offset(),
                        s.text,
                        f"asr {engine.name}",
                    )
                )
        except MissingTool as e:
            if args.asr:
                raise
            env.warnings.append(f"no subtitles and no speech engine: {e.hint}")

    rows = []
    for start, end, text, origin in speech:
        rows.append(
            finding(
                Src(args.source, t=(start, end)), type="speech", text=text, path=None, source=origin
            )
        )

    duplicates = 0
    if video is not None and not args.no_frames and not args.subs_only and ffmpeg.has_video(video):
        # The glob would pick up frames from an earlier run and give them wrong times
        shutil.rmtree(work / "frames", ignore_errors=True)
        frames = [
            (p, t, "scene")
            for p, t in ffmpeg.scene_frames(
                video, work / "frames", float(args.scene or conf.get("scene", 0.3)), window
            )
        ] + [
            (p, t, "interval")
            for p, t in ffmpeg.interval_frames(
                video, work / "frames", float(args.fps or conf.get("fps", 1.0)), window
            )
        ]
        frames.sort(key=lambda f: (f[1], f[2] != "scene"))
        from PIL import Image

        kept: list[tuple[imaging.Look, Path]] = []
        limit = int(conf.get("dedup_distance", 4))
        for path, t, kind in frames:
            look = imaging.Look.of(Image.open(path))
            # Compare only with the last kept frame, so a scene that returns later shows up again
            dup = kept[-1][1] if kept and look.same_as(kept[-1][0], limit) else None
            if dup is not None and not args.keep_duplicates:
                duplicates += 1
                continue
            kept.append((look, path))
            rows.append(
                finding(
                    Src(args.source, t=(t, t)),
                    type="frame",
                    text=kind,
                    path=str(path),
                    dup_of=str(dup) if dup else None,
                )
            )

    rows.sort(key=lambda r: (r["src"]["t"][0], r["type"]))
    env.results = rows
    transcript = work / "transcript.txt"
    if speech:
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text(
            "\n".join(
                f"[{r['cite'].rsplit('@', 1)[1]}] {r['text']}"
                for r in rows
                if r["type"] == "speech"
            )
            + "\n",
            encoding="utf-8",
        )
        env.artifact(str(transcript), "transcript")
    frames_n = sum(r["type"] == "frame" for r in rows)
    env.summary = (
        f"{len(speech)} speech segments, {frames_n} frames, "
        f"{duplicates} near-duplicate frames skipped"
    )
    return env
