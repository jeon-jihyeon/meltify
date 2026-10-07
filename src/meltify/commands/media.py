from __future__ import annotations

import argparse
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from meltify import __version__, video
from meltify.config import pick
from meltify.evidence import Envelope, Src, finding
from meltify.files import flat_name, work_name
from meltify.safe import MissingTool

if TYPE_CHECKING:
    from meltify.ffmpeg import Window

NAME = "media"
HELP = "turn a video, recording or video URL into timestamped frames and a timestamped transcript"
COLUMNS = ["type", "text", "path", "cite"]


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
    p.add_argument(
        "--allow-private",
        action="store_true",
        help="allow URLs that resolve to private addresses",
    )


Speech = tuple[float, float, str, str]


def run(args: argparse.Namespace, settings: dict[str, Any]) -> Envelope:
    from meltify import ffmpeg

    env = Envelope(command=NAME, version=__version__)
    conf = settings["media"]
    scene = float(pick(args.scene, conf["scene"]))
    fps = float(pick(args.fps, conf["fps"]))
    frames_wanted = not (args.no_frames or args.subs_only)
    if frames_wanted and fps <= 0:
        raise ValueError(f"--fps must be above 0, got {fps:g}")
    if not 0 <= scene <= 1:
        raise ValueError(f"--scene must be between 0 and 1, got {scene:g}")
    window = ffmpeg.Window(args.start, args.end)
    clip, subs, work = _input(args, conf, Path(settings["out_dir"]) / "media")
    env.inputs.append({"path": args.source})

    speech = _speech(args, settings, clip, subs, work / "audio.wav", window, env.warnings.append)
    rows = [
        finding(
            Src(args.source, t=(start, end)), type="speech", text=text, path=None, source=origin
        )
        for start, end, text, origin in speech
    ]
    duplicates = 0
    if clip is not None and frames_wanted and ffmpeg.has_video(clip):
        frames, duplicates = _frames(args, conf, clip, work / "frames", scene, fps, window)
        rows += frames

    rows.sort(key=lambda r: (r["src"]["t"][0], r["type"]))
    env.results = rows
    if speech:
        env.artifact(str(_write_transcript(rows, work / "transcript.txt")), "transcript")
    frames_n = sum(r["type"] == "frame" for r in rows)
    env.summary = (
        f"{len(speech)} speech segments, {frames_n} frames, "
        f"{duplicates} near-duplicate frames skipped"
    )
    return env


def _input(
    args: argparse.Namespace, conf: dict[str, Any], base: Path
) -> tuple[Path | None, list[Path], Path]:
    """The clip, its subtitle files and the work folder, downloading a URL first"""
    if args.source.startswith(("http://", "https://")):
        work = base / work_name(args.source)
        clip, subs = video.download(
            args.source,
            work,
            list(conf["sub_langs"]),
            args.subs_only,
            int(conf["max_height"]),
            args.allow_private,
        )
        return clip, subs, work
    clip = Path(args.source)
    if not clip.is_file():
        raise FileNotFoundError(clip)
    return clip, video.sidecars(clip), base / flat_name(clip)


def _speech(
    args: argparse.Namespace,
    settings: dict[str, Any],
    clip: Path | None,
    subs: list[Path],
    wav: Path,
    window: Window,
    warn: Callable[[str], None],
) -> list[Speech]:
    """Timed lines from the subtitles, else from a speech engine"""
    from meltify import ffmpeg
    from meltify.engines import asr

    if found := video.subtitles(subs, args.start, args.end):
        sub, cues = found
        return [(start, end, text, f"subtitle {sub.name}") for start, end, text in cues]
    if args.subs_only or clip is None:
        return []
    try:
        engine = asr.select(pick(args.asr, settings["asr"]["engine"]), settings)
        audio = ffmpeg.audio(clip, wav, window)
        return [
            (s.start + window.offset(), s.end + window.offset(), s.text, f"asr {engine.name}")
            for s in engine.transcribe(audio, str(settings["asr"]["lang"]))
        ]
    except MissingTool as e:
        if args.asr is not None:
            raise
        warn(f"no subtitles and no speech engine: {e.hint}")
        return []


def _frames(
    args: argparse.Namespace,
    conf: dict[str, Any],
    clip: Path,
    folder: Path,
    scene: float,
    fps: float,
    window: Window,
) -> tuple[list[dict[str, Any]], int]:
    """Frame rows for scene changes and intervals, and how many near-duplicates were dropped"""
    from meltify import ffmpeg, imaging

    # The glob would pick up frames from an earlier run and give them wrong times
    shutil.rmtree(folder, ignore_errors=True)
    frames = [(p, t, "scene") for p, t in ffmpeg.scene_frames(clip, folder, scene, window)] + [
        (p, t, "interval") for p, t in ffmpeg.interval_frames(clip, folder, fps, window)
    ]
    frames.sort(key=lambda f: (f[1], f[2] != "scene"))
    when = {p: (t, kind) for p, t, kind in frames}
    distinct = imaging.distinct_frames(
        [p for p, _, _ in frames], int(conf["dedup_distance"]), args.keep_duplicates
    )
    rows = []
    for path, dup in distinct:
        t, kind = when[path]
        rows.append(
            finding(
                Src(args.source, t=(t, t)),
                type="frame",
                text=kind,
                path=str(path),
                dup_of=str(dup) if dup else None,
            )
        )
    return rows, len(frames) - len(rows)


def _write_transcript(rows: list[dict[str, Any]], transcript: Path) -> Path:
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(
        "\n".join(
            f"[{r['cite'].rsplit('@', 1)[1]}] {r['text']}" for r in rows if r["type"] == "speech"
        )
        + "\n",
        encoding="utf-8",
    )
    return transcript
