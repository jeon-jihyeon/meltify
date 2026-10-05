import json
import shutil
from pathlib import Path

import pytest

from meltify import ffmpeg
from meltify.cli import main
from meltify.commands.media import _sidecars, _work_name, parse_subtitles
from meltify.engines import asr
from meltify.engines.asr import Segment
from tests.fixtures.make_media import VTT, scenes_video

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


class FakeAsr:
    name = "fake"

    def missing(self):
        return None

    def transcribe(self, audio, lang):
        assert Path(audio).is_file()
        return [Segment(0.2, 1.0, "hello there")]


def test_parse_subtitles_strips_tags_and_rolling_repeats():
    assert parse_subtitles(VTT) == [
        (0.5, 1.8, "first words"),
        (1.8, 3.0, "more words"),
        (4.2, 5.5, "blue part"),
    ]
    srt = "1\n00:00:01,000 --> 00:00:02,500\nhola\n"
    assert parse_subtitles(srt) == [(1.0, 2.5, "hola")]


@needs_ffmpeg
def test_scene_frames_and_sidecar_subtitles(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    video = scenes_video(tmp_path / "clip.mp4")
    (tmp_path / "clip.vtt").write_text(VTT)
    assert main(["media", str(video), "--json", "--limit", "0"]) == 0
    out = json.loads(capsys.readouterr().out)
    frames = [r for r in out["results"] if r["type"] == "frame"]
    scene_times = [r["src"]["t"][0] for r in frames if r["text"] == "scene"]
    assert [round(t) for t in scene_times] == [0, 2, 4]
    # A solid color holds still, so interval frames inside a scene are dropped as duplicates
    assert len(frames) == 3
    speech = [r for r in out["results"] if r["type"] == "speech"]
    assert [r["text"] for r in speech] == ["first words", "more words", "blue part"]
    assert speech[0]["cite"] == f"{video}@00:00:00.5-00:00:01.8"
    transcript = Path(next(a["path"] for a in out["artifacts"] if a["role"] == "transcript"))
    assert transcript.read_text().splitlines()[0] == "[00:00:00.5-00:00:01.8] first words"


@needs_ffmpeg
def test_window_and_asr_offsets(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(asr, "select", lambda spec, s: FakeAsr())
    video = scenes_video(tmp_path / "clip.mp4")
    assert main(["media", str(video), "--start", "3", "--end", "6", "--json", "--limit", "0"]) == 0
    out = json.loads(capsys.readouterr().out)
    frames = [r for r in out["results"] if r["type"] == "frame"]
    assert all(3 <= r["src"]["t"][0] <= 6 for r in frames)
    assert any(round(r["src"]["t"][0]) == 4 and r["text"] == "scene" for r in frames)
    speech = [r for r in out["results"] if r["type"] == "speech"]
    assert speech[0]["src"]["t"] == [3.2, 4.0]
    assert speech[0]["source"] == "asr fake"


@needs_ffmpeg
def test_missing_speech_engine_warns_but_keeps_frames(tmp_path, monkeypatch, capsys):
    from meltify.safe import MissingTool

    def none(spec, s):
        raise MissingTool("speech engine", "install one")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(asr, "select", none)
    video = scenes_video(tmp_path / "clip.mp4")
    assert main(["media", str(video), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert any("no subtitles and no speech engine" in w for w in out["warnings"])
    assert main(["media", str(video), "--asr", "mlx", "--json"]) == 3


@needs_ffmpeg
def test_file_named_like_an_option_is_still_a_file(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(asr, "select", lambda spec, s: FakeAsr())
    scenes_video(Path("-v.mp4"))
    assert main(["media", "./-v.mp4", "--json", "--limit", "0"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert sum(r["type"] == "frame" for r in out["results"]) == 3


@needs_ffmpeg
def test_unreadable_video_is_an_error_not_zero_frames(tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video")
    with pytest.raises(RuntimeError):
        ffmpeg.has_video(bad)


def test_sidecars_match_only_this_video(tmp_path):
    video = tmp_path / "clip[1].mp4"
    names = ["clip[1].vtt", "clip[1].en.srt", "clip[1]2.vtt", "clip1.vtt", "clip[1].mp4.txt"]
    for name in [video.name, *names]:
        (tmp_path / name).write_text("")
    assert [p.name for p in _sidecars(video)] == ["clip[1].en.srt", "clip[1].vtt"]


def test_url_work_dirs_do_not_collide():
    tail = "x" * 100
    assert _work_name(f"https://a.com/{tail}") != _work_name(f"https://b.com/{tail}")


def test_missing_file_is_a_usage_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["media", str(tmp_path / "none.mp4")]) == 2
