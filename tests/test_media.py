import json
import shutil
from pathlib import Path

import pytest

from meltify import ffmpeg
from meltify.cli import main
from meltify.converters.subtitle import parse_subtitles
from meltify.engines import asr
from meltify.engines import ocr as engines
from meltify.engines.asr import Segment
from meltify.files import work_name
from meltify.safe import MissingTool
from meltify.video import sidecars
from tests.fixtures.make_media import VTT, scenes_video

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


class FakeAsr:
    name = "fake"

    def missing(self):
        return None

    def transcribe(self, audio, lang):
        assert Path(audio).is_file()
        # Speech language is detected by default, whatever the OCR lang says
        assert lang == "auto"
        return [Segment(0.2, 1.0, "hello there")]


def _no_speech(spec, s, down=frozenset()):
    raise MissingTool("speech engine", "install one")


@pytest.fixture
def clip(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # Frames are still listed without an OCR engine, so no test here depends on one
    monkeypatch.setattr(engines, "select", lambda spec, s: [])
    return tmp_path / "clip.mp4"


def _read(capsys, *args):
    code = main(["read", *args, "--json", "--limit", "0"])
    out = json.loads(capsys.readouterr().out)
    frames = [r for r in out["results"] if r["kind"] == "frame"]
    return code, out, frames


def test_parse_subtitles_strips_tags_and_rolling_repeats():
    assert parse_subtitles(VTT) == [
        (0.5, 1.8, "first words"),
        (1.8, 3.0, "more words"),
        (4.2, 5.5, "blue part"),
    ]
    srt = "1\n00:00:01,000 --> 00:00:02,500\nhola\n"
    assert parse_subtitles(srt) == [(1.0, 2.5, "hola")]


@needs_ffmpeg
def test_scene_frames_and_sidecar_subtitles(clip, monkeypatch, capsys):
    monkeypatch.setattr(
        asr, "select", lambda spec, s, down=frozenset(): pytest.fail("subtitles stand in for ASR")
    )
    scenes_video(clip)
    clip.with_suffix(".vtt").write_text(VTT)
    code, out, frames = _read(capsys, str(clip))
    assert code == 0
    assert [(round(r["src"]["t"][0]), r["reasons"][0]) for r in frames] == [
        (0, "scene"),
        (2, "scene"),
        (4, "scene"),
    ]
    assert all(
        Path(r["path"]).is_file()
        and "/attachments/clip.mp4/frames/" in r["path"]
        and r["path"].endswith(".webp")
        for r in frames
    )
    md = Path(out["results"][0]["out"]).read_text()
    assert "@00:00:00.5-00:00:01.8| first words" in md
    assert "3 frames" in out["summary"]


@needs_ffmpeg
def test_window_and_asr_offsets(clip, monkeypatch, capsys):
    monkeypatch.setattr(asr, "select", lambda spec, s, down=frozenset(): FakeAsr())
    scenes_video(clip)
    code, out, frames = _read(capsys, str(clip), "--start", "3", "--end", "6")
    assert code == 0
    assert frames and all(3 <= r["src"]["t"][0] <= 6 for r in frames)
    assert any(round(r["src"]["t"][0]) == 4 and r["reasons"] == ["scene"] for r in frames)
    md = Path(out["results"][0]["out"]).read_text()
    assert "@00:00:03.2-00:00:04.0| hello there" in md


@needs_ffmpeg
def test_interval_frames_and_kept_duplicates(clip, monkeypatch, capsys):
    monkeypatch.setattr(asr, "select", _no_speech)
    scenes_video(clip)
    # A solid color holds still, so interval frames inside a scene are dropped as duplicates
    _, _, frames = _read(capsys, str(clip), "--fps", "1")
    assert [r["reasons"] for r in frames] == [["scene"], ["scene"], ["scene"]]
    _, _, frames = _read(capsys, str(clip), "--fps", "1", "--keep-duplicates")
    repeats = [r for r in frames if len(r["reasons"]) > 1]
    assert len(frames) > 3 and repeats
    assert repeats[0]["reasons"][0] == "interval"
    assert repeats[0]["reasons"][1].startswith("repeats 00:00:0")


@needs_ffmpeg
def test_frames_flag_caps_and_zero_skips_frames(clip, monkeypatch, capsys):
    monkeypatch.setattr(asr, "select", _no_speech)
    scenes_video(clip)
    assert len(_read(capsys, str(clip), "--frames", "2")[2]) == 2
    assert _read(capsys, str(clip), "--frames", "0")[2] == []


@needs_ffmpeg
def test_missing_speech_engine_warns_but_keeps_frames(clip, monkeypatch, capsys):
    monkeypatch.setattr(asr, "select", _no_speech)
    scenes_video(clip)
    code, out, frames = _read(capsys, str(clip))
    assert code == 0 and len(frames) == 3
    assert any("no speech engine" in w for w in out["warnings"])
    assert main(["read", str(clip), "--asr", "whispercpp", "--json"]) == 3


@needs_ffmpeg
def test_subs_only_takes_subtitles_and_nothing_else(clip, monkeypatch, capsys):
    monkeypatch.setattr(
        asr, "select", lambda spec, s, down=frozenset(): pytest.fail("no ASR with --subs-only")
    )
    scenes_video(clip)
    code, out, frames = _read(capsys, str(clip), "--subs-only")
    assert code == 0 and frames == []
    assert out["results"][0]["needs"] == ["no subtitles"]
    clip.with_suffix(".vtt").write_text(VTT)
    code, out, frames = _read(capsys, str(clip), "--subs-only")
    assert frames == [] and out["results"][0]["needs"] == []
    assert "blue part" in Path(out["results"][0]["out"]).read_text()


@needs_ffmpeg
def test_file_named_like_an_option_is_still_a_file(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(engines, "select", lambda spec, s: [])
    monkeypatch.setattr(asr, "select", lambda spec, s, down=frozenset(): FakeAsr())
    scenes_video(Path("-v.mp4"))
    assert len(_read(capsys, "./-v.mp4")[2]) == 3


@needs_ffmpeg
def test_unreadable_video_is_an_error_not_zero_frames(tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video")
    with pytest.raises(RuntimeError):
        ffmpeg.has_audio(bad)


def test_sidecars_match_only_this_video(tmp_path):
    video = tmp_path / "clip[1].mp4"
    names = ["clip[1].vtt", "clip[1].en.srt", "clip[1]2.vtt", "clip1.vtt", "clip[1].mp4.txt"]
    for name in [video.name, *names]:
        (tmp_path / name).write_text("")
    assert [p.name for p in sidecars(video)] == ["clip[1].en.srt", "clip[1].vtt"]


def test_url_work_dirs_do_not_collide():
    tail = "x" * 100
    assert work_name(f"https://a.com/{tail}") != work_name(f"https://b.com/{tail}")


def test_scene_zero_is_a_value_not_the_default(clip, monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(asr, "select", _no_speech)
    monkeypatch.setattr(ffmpeg, "has_audio", lambda path: False)
    monkeypatch.setattr(ffmpeg, "scene_frames", lambda p, d, scene, w: seen.append(scene) or [])
    clip.write_bytes(b"x")
    assert _read(capsys, str(clip), "--scene", "0")[0] == 0
    assert seen == [0.0]


def test_a_window_that_ends_first_is_a_usage_error(clip, capsys):
    clip.write_bytes(b"x")
    code, out, _ = _read(capsys, str(clip), "--start", "5", "--end", "2")
    assert code == 2 and "--start 5 must come before --end 2" in out["errors"][0]["message"]


@pytest.mark.parametrize(
    "flag,value,message",
    [
        ("--fps", "0", "must be above 0"),
        ("--upscale", "0", "must be above 0"),
        ("--scene", "-1", "must be between 0 and 1"),
        ("--pages", "5-3", "reversed page range 5-3"),
    ],
)
def test_bad_values_are_refused_by_the_parser(clip, capsys, flag, value, message):
    with pytest.raises(SystemExit) as e:
        main(["read", str(clip), flag, value])
    assert e.value.code == 2
    assert message in capsys.readouterr().err


def test_the_api_refuses_what_the_parser_would(tmp_path, monkeypatch):
    import meltify

    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.txt").write_text("x")
    with pytest.raises(ValueError, match="fps: must be above 0"):
        meltify.read("a.txt", fps=0)
    with pytest.raises(ValueError, match="compare must be one of numbers, tokens"):
        meltify.read("a.txt", compare="words")
    with pytest.raises(ValueError, match="bad page spec"):
        meltify.read("a.txt", pages="x")
    # A number is the page range it spells, and one reading stands for a list of one
    (tmp_path / "mine.txt").write_text("x\n")
    with pytest.raises(ValueError, match="--reading needs exactly one image"):
        meltify.read("a.txt", pages=2, reading="agent=mine.txt")


def test_a_url_to_a_private_address_never_reaches_yt_dlp(tmp_path, monkeypatch, capsys):
    import socket
    import sys

    monkeypatch.chdir(tmp_path)
    real = socket.getaddrinfo
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *a, **k: real("10.0.0.7", port, type=socket.SOCK_STREAM),
    )
    # A None module makes any import of yt_dlp fail, so reaching it would show up as an error
    monkeypatch.setitem(sys.modules, "yt_dlp", None)
    _, out, _ = _read(capsys, "http://intranet.test/clip.mp4")
    [row] = out["results"]
    assert "10.0.0.7 isn't a public address" in row["error"]


def test_a_failure_inside_a_command_still_prints_the_envelope(tmp_path, monkeypatch, capsys):
    from meltify.melt import Reader

    def boom(self, recognize):
        raise RuntimeError("worker died")

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MELTIFY_DEBUG", raising=False)
    monkeypatch.setattr(Reader, "finish", boom)
    (tmp_path / "a.txt").write_text("x")
    assert main(["read", "a.txt", "--json"]) == 1
    captured = capsys.readouterr()
    [error] = json.loads(captured.out)["errors"]
    assert error["code"] == "failed" and error["message"] == "RuntimeError: worker died"
    assert "MELTIFY_DEBUG" in error["hint"] and "Traceback" not in captured.err

    monkeypatch.setenv("MELTIFY_DEBUG", "1")
    assert main(["read", "a.txt", "--json"]) == 1
    assert "Traceback" in capsys.readouterr().err


@needs_ffmpeg
def test_frames_over_the_cap_are_reported(clip, monkeypatch, capsys):
    monkeypatch.setattr(asr, "select", _no_speech)
    scenes_video(clip)
    _, out, frames = _read(capsys, str(clip), "--fps", "2", "--keep-duplicates", "--frames", "4")
    assert len(frames) == 4
    assert any("kept 4 of " in w and "raise --frames" in w for w in out["warnings"])
    # The cached listing still knows how many there were
    _, out, _ = _read(capsys, str(clip), "--fps", "2", "--keep-duplicates", "--frames", "4")
    assert any("kept 4 of " in w for w in out["warnings"])


@needs_ffmpeg
def test_kept_duplicates_never_crowd_out_a_scene(clip, monkeypatch, capsys):
    monkeypatch.setattr(asr, "select", _no_speech)
    scenes_video(clip)
    args = (str(clip), "--fps", "2", "--frames", "3", "--keep-duplicates")
    _, _, frames = _read(capsys, *args)
    assert [(round(r["src"]["t"][0]), r["reasons"]) for r in frames] == [
        (0, ["scene"]),
        (2, ["scene"]),
        (4, ["scene"]),
    ]


@needs_ffmpeg
def test_media_under_an_odd_name_honors_subs_only(clip, monkeypatch, capsys):
    monkeypatch.setattr(
        asr, "select", lambda spec, s, down=frozenset(): pytest.fail("no ASR with --subs-only")
    )
    scenes_video(clip)
    odd = clip.with_suffix(".dat")
    clip.rename(odd)
    _, out, frames = _read(capsys, str(odd), "--subs-only")
    assert frames == [] and out["results"][0]["needs"] == ["no subtitles"]


@needs_ffmpeg
def test_frames_survive_a_read_only_cache(clip, monkeypatch, capsys):
    cache = clip.parent / "ro-cache"
    cache.mkdir()
    cache.chmod(0o555)
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    monkeypatch.setattr(asr, "select", _no_speech)
    scenes_video(clip)
    try:
        code, _, frames = _read(capsys, str(clip))
    finally:
        cache.chmod(0o755)
    assert code == 0 and len(frames) == 3
    assert all(Path(r["path"]).is_file() for r in frames)


@needs_ffmpeg
def test_a_rerun_clears_the_frames_it_no_longer_keeps(clip, monkeypatch, capsys):
    monkeypatch.setattr(asr, "select", _no_speech)
    scenes_video(clip)
    _read(capsys, str(clip))
    folder = clip.parent / "meltify-out/read/attachments/clip.mp4/frames"
    assert len(list(folder.iterdir())) == 3
    _read(capsys, str(clip), "--frames", "1")
    assert len(list(folder.iterdir())) == 1


@needs_ffmpeg
def test_a_video_in_a_zip_finds_its_subtitles_whatever_the_order(clip, monkeypatch, capsys):
    import zipfile

    monkeypatch.setattr(
        asr, "select", lambda spec, s, down=frozenset(): pytest.fail("subtitles stand in for ASR")
    )
    scenes_video(clip)
    with zipfile.ZipFile(clip.parent / "a.zip", "w") as z:
        z.write(clip, "clip.mp4")
        z.writestr("clip.vtt", VTT)
    _, out, _ = _read(capsys, "a.zip", "--frames", "0", "--jobs", "4")
    media = next(r for r in out["results"] if r["cite"] == "a.zip#att=clip.mp4")
    assert "first words" in Path(media["out"]).read_text()
