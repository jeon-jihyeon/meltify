import io
import json
import re
import shutil
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from meltify import config, imaging
from meltify.cli import main
from meltify.converters import embeds, metafile, office, quicklook, render, run, sheet
from meltify.engines import asr
from meltify.engines import ocr as engines
from meltify.engines.asr import Segment
from meltify.engines.ocr import LOCAL, TextBox
from meltify.evidence import Src
from meltify.safe import MissingTool, Unreachable
from tests.fixtures.make_docs import (
    card,
    embedded_docx,
    embedded_pdf,
    embedded_pptx,
    embedded_xlsx,
    inline_image_mail,
)

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
BOX = re.compile(r"@(pt|px)\(([^)]*)\)")


@dataclass
class Fake:
    """Reads fixed lines, boxed in the top left quarter of whatever it's shown"""

    name: str
    lines: list[str]
    kind: str = LOCAL
    calls: list[tuple[int, int]] = field(default_factory=list)

    def missing(self):
        return None

    def recognize(self, image, size):
        self.calls.append(size)
        w, h = size
        return [
            TextBox(t, (0.1 * w, 0.1 * h + 0.1 * h * i, 0.5 * w, 0.18 * h + 0.1 * h * i), 0.9)
            for i, t in enumerate(self.lines)
        ]


class FakeAsr:
    name = "fake"
    model = "tiny"

    def __init__(self):
        self.calls = 0

    def missing(self):
        return None

    def transcribe(self, audio, lang):
        self.calls += 1
        self.lang = lang
        return [Segment(0.5, 1.5, "안녕하세요 회의 시작합니다")]


@pytest.fixture
def cached(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _engines(monkeypatch, *fakes):
    monkeypatch.setattr(engines, "select", lambda spec, s: list(fakes))
    return fakes


def _read(capsys, *args):
    code = main(["read", *map(str, args), "--json", "--limit", "0"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    return {r["cite"]: r for r in out["results"]}, out


def _box(cite):
    unit, nums = BOX.search(cite).groups()
    return unit, [float(v) for v in nums.split(",")]


def test_auto_factor_lifts_small_images_and_caps_big_ones():
    assert imaging.auto_factor(300, 100) == 4.0
    assert imaging.auto_factor(900, 300) == pytest.approx(3.333, abs=0.001)
    assert imaging.auto_factor(1500, 1200) == 1.0
    assert imaging.auto_factor(8000, 2000) == 0.5
    assert max(v * imaging.auto_factor(2000, 600) for v in (2000, 600)) <= 4000


def test_pdf_embedded_images_cite_inside_their_rect_and_skip_decoration(
    cached, monkeypatch, capsys
):
    vision, paddle = _engines(
        monkeypatch, Fake("vision", ["INVOICE 2026-0917"]), Fake("paddle", ["INVOICE 2026-0917"])
    )
    pdf = embedded_pdf(cached / "report.pdf")
    rows, out = _read(capsys, pdf)
    row = rows[str(pdf)]
    assert row["needs"] == []
    md = Path(row["out"]).read_text()

    # Only the invoice picture and the scanned page reach the engines
    assert len(vision.calls) == 2
    assert f"## {pdf}#p1#img1\n" in md
    assert f"## {pdf}#p3\n" in md
    assert "#p2#img" not in md and "#p4#img" not in md
    # Recognized text lands right after the page it belongs to
    assert md.index(f"## {pdf}#p1\n") < md.index(f"## {pdf}#p1#img1") < md.index(f"## {pdf}#p2\n")

    line = next(x for x in md.splitlines() if x.endswith("INVOICE 2026-0917") and "@pt" in x)
    unit, (x0, y0, x1, y1) = _box(line)
    assert unit == "pt"
    assert 72 <= x0 < x1 <= 522 and 100 <= y0 < y1 <= 250
    # 10% into a 450 by 150 pt rect
    assert (x0, y0) == pytest.approx((117, 115), abs=0.5)

    scan = md[md.index(f"## {pdf}#p3\n") :].splitlines()[1]
    unit, (x0, y0, x1, y1) = _box(scan)
    assert unit == "pt" and x1 <= 595.5 and y1 <= 842.5


def test_same_picture_in_several_files_reads_once_and_never_on_rerun(cached, monkeypatch, capsys):
    vision, paddle = _engines(
        monkeypatch, Fake("vision", ["TOTAL 655"]), Fake("paddle", ["TOTAL 655"])
    )
    image = card("TOTAL 655", (400, 120))
    folder = cached / "in"
    folder.mkdir()
    (folder / "a.png").write_bytes(image)
    embedded_docx(folder / "memo.docx", image)
    embedded_xlsx(folder / "book.xlsx", image)
    inline_image_mail(folder / "mail.eml", image, card("Q3 4,210", (300, 100)))

    rows, out = _read(capsys, folder)
    # One unique invoice picture and the mail's chart attachment
    assert (len(vision.calls), len(paddle.calls)) == (2, 2)
    assert "(0 cached)" in out["summary"]

    rows, out = _read(capsys, folder)
    assert (len(vision.calls), len(paddle.calls)) == (2, 2)
    assert "(2 cached)" in out["summary"]
    # The office extra is optional, so its own need may or may not be listed
    leftover = [n for r in rows.values() for n in r["needs"] if n != "markitdown"]
    assert all("not read" in n for n in leftover)

    _read(capsys, folder, "--refresh")
    assert len(vision.calls) == 4


def _no_drawing(monkeypatch):
    """No program on this machine draws an EMF, whatever is installed"""
    monkeypatch.setattr(render, "soffice", lambda: None)
    monkeypatch.setattr(quicklook, "available", lambda: False)
    monkeypatch.setattr(metafile, "replay_available", lambda: False)


def test_office_images_cite_paragraph_slide_and_cell(tmp_path, monkeypatch):
    _no_drawing(monkeypatch)
    image = card("INVOICE", (400, 120))
    docx = office.docx_images(embedded_docx(tmp_path / "memo.docx", image), Src("memo.docx"))
    docx.draw()
    assert [j.src.cite() for j in docx.jobs] == ["memo.docx#para3#img1"]
    assert sorted(docx.needs()) == [
        f"1 emf image not read ({embeds.DRAW_HINT})",
        "1 linked image not read",
    ]

    pytest.importorskip("pptx")
    deck = office.pptx_images(embedded_pptx(tmp_path / "deck.pptx", image), Src("deck.pptx"))
    deck.draw()
    assert [j.src.cite() for j in deck.jobs] == ["deck.pptx#slide3#img2"]

    book = sheet.images(embedded_xlsx(tmp_path / "book.xlsx", image), Src("book.xlsx"))
    assert [j.src.cite() for j in book.jobs] == ["book.xlsx#Sales!D2#img1"]
    assert book.jobs[0].data == image


def test_disputed_and_unchecked_markdown(cached, monkeypatch, capsys):
    _engines(
        monkeypatch,
        Fake("vision", ["TOTAL 655", "총 1,250"]),
        Fake("paddle", ["TOTAL 665", "총 1,250"]),
    )
    (cached / "menu.png").write_bytes(card("TOTAL 655", (400, 100)))
    rows, out = _read(capsys, cached / "menu.png", "--no-pictures")
    md = Path(rows[f"{cached}/menu.png"]["out"]).read_text()
    assert md == (
        f"<!-- meltify source: {cached}/menu.png -->\n"
        f"## {cached}/menu.png\n"
        "@px(40,10,200,18)| TOTAL 655\n"
        "@px(40,20,200,28)| 총 1,250\n"
        "> disputed 655: vision 1, paddle 0\n"
        "> disputed 665: vision 0, paddle 1\n\n"
    )
    disputed = [r for r in out["results"] if r["kind"] == "disputed"]
    assert [(r["value"], r["counts"]) for r in disputed] == [
        ("655", {"vision": 1, "paddle": 0}),
        ("665", {"vision": 0, "paddle": 1}),
    ]
    assert disputed[0]["cite"] == f"{cached}/menu.png@px(40,10,200,18)"
    index = Path(out["artifacts"][0]["path"]).read_text()
    assert index.count('"kind": "disputed"') == 2

    _engines(monkeypatch, Fake("paddle", ["TOTAL 655"]))
    rows, _ = _read(capsys, cached / "menu.png", "--refresh")
    md = Path(rows[f"{cached}/menu.png"]["out"]).read_text()
    assert md.endswith("@px(40,10,200,18)| TOTAL 655\n> unchecked: one engine\n\n")


def test_budget_leaves_the_rest_as_needs_and_a_rerun_converges(cached, monkeypatch, capsys):
    (vision,) = _engines(monkeypatch, Fake("vision", ["A1"]))
    pdf = embedded_pdf(cached / "report.pdf")
    (cached / "scan.png").write_bytes(card("B2", (200, 80)))
    rows, _ = _read(capsys, pdf, cached / "scan.png", "--budget", "0")
    assert vision.calls == []
    assert rows[str(pdf)]["needs"] == ["ocr pages 3 (budget)", "ocr (budget)"]
    assert rows[f"{cached}/scan.png"]["needs"] == ["ocr (budget)"]

    _read(capsys, pdf, cached / "scan.png")
    rows, _ = _read(capsys, pdf, cached / "scan.png", "--budget", "0")
    assert len(vision.calls) == 3
    assert rows[str(pdf)]["needs"] == [] and rows[f"{cached}/scan.png"]["needs"] == []


def test_auto_never_picks_paid_engines(cached, monkeypatch):
    from meltify import config
    from meltify.recognize import Options, Recognizer

    for key in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.setenv(key, "set")
    settings = config.load()
    rec = Recognizer(Options(), settings, lambda m: None)
    assert {e.name for e in rec.ocr_engines()} <= {"vision"}

    monkeypatch.setattr(asr.WhisperCpp, "missing", lambda self: "no")
    rec = Recognizer(Options(), settings, lambda m: None)
    assert rec.asr_engine() is None
    with pytest.raises(MissingTool):
        Recognizer(Options(asr="whispercpp"), settings, lambda m: None).asr_engine()


def test_a_speech_endpoint_you_serve_goes_first_and_a_broken_one_warns(cached, monkeypatch):
    from meltify.recognize import Options, Recognizer

    monkeypatch.setattr(asr.WhisperCpp, "missing", lambda self: None)
    settings = config.load()
    table = {"base_url": "http://127.0.0.1:9000/v1", "model": "whisper"}
    settings["asr"]["endpoints"] = {"served": table}
    assert Recognizer(Options(), settings, lambda m: None).asr_engine().name == "served"

    table["base_url"] = "https://api.openai.com/v1"
    warned: list[str] = []
    assert Recognizer(Options(), settings, warned.append).asr_engine().name == "whispercpp"
    assert any("asr endpoint served is skipped" in w for w in warned)


def test_a_server_that_stops_answering_is_asked_once(cached, monkeypatch):
    from meltify.recognize import Options, Recognizer

    calls = []

    @dataclass
    class Down:
        name: str = "vl"
        kind: str = LOCAL

        def missing(self):
            return None

        def recognize(self, image, size):
            calls.append(image)
            raise Unreachable("nothing answers at http://127.0.0.1:9/v1")

    monkeypatch.setattr(engines, "ready", lambda spec, settings: [Down()])
    warned: list[str] = []
    rec = Recognizer(Options(), config.defaults(), warned.append)
    data = card("PAGE 1", (200, 60))

    def load():
        from PIL import Image

        return Image.open(io.BytesIO(data))

    assert rec.picture("sha0", load, "a.png", "image", True).readings == []
    # With the only engine down, later pictures stay needs instead of waiting on it again
    for n in (1, 2):
        with pytest.raises(MissingTool):
            rec.picture(f"sha{n}", load, f"{n}.png", "image", True)
    assert len(calls) == 1
    assert warned == [
        "vl is skipped for the rest of this run: nothing answers at http://127.0.0.1:9/v1"
    ]


def test_a_speech_server_that_stops_hands_over_to_the_next_engine(cached, monkeypatch, tmp_path):
    from meltify.recognize import Options, Recognizer

    @dataclass
    class Talks:
        name: str
        down: bool = False

        def missing(self):
            return None

        def transcribe(self, audio, lang):
            if self.down:
                raise Unreachable("nothing answers at http://127.0.0.1:9000/v1")
            return [Segment(0, 1, "hello")]

    served, local = Talks("served", down=True), Talks("whispercpp")

    def select(spec, settings, down=frozenset()):
        return local if "served" in down else served

    monkeypatch.setattr(asr, "select", select)
    monkeypatch.setattr("meltify.ffmpeg.has_audio", lambda path: True)
    monkeypatch.setattr("meltify.ffmpeg.audio", lambda path, out, window: out)
    rec = Recognizer(Options(), config.defaults(), lambda m: None)
    clip = tmp_path / "a.m4a"
    clip.write_bytes(b"audio")
    assert rec.speech("sha", clip, "a.m4a", True) == [Segment(0, 1, "hello")]
    assert rec.asr_engine() is local


def test_speech_endpoints_take_no_prompt():
    settings = config.defaults()
    table = {"base_url": "http://127.0.0.1:9000/v1", "model": "whisper", "prompt": "x"}
    settings["asr"]["endpoints"] = {"served": table}
    with pytest.raises(ValueError, match="unknown field prompt"):
        asr.endpoints(settings)


def test_cache_keys_follow_the_model_and_server(cached):
    from meltify.recognize import _identity

    gpt = engines.build("openai", config.defaults())
    served = config.defaults()
    served["llm"]["openai_model"] = "another-model"
    assert _identity(gpt) != _identity(engines.build("openai", served))
    assert _identity(engines.Vision("ko")) == "vision"


def test_no_engine_keeps_the_01_needs_and_skips_loading(cached, monkeypatch, capsys):
    _engines(monkeypatch)
    (cached / "a.png").write_bytes(card("X", (200, 80)))
    rows, out = _read(capsys, cached / "a.png")
    assert rows[f"{cached}/a.png"]["needs"] == ["ocr"]
    assert any("no local OCR engine" in w for w in out["warnings"])

    called = []
    monkeypatch.setattr(engines, "select", lambda spec, s: called.append(spec) or [])
    (cached / "b.txt").write_text("plain\n")
    _read(capsys, cached / "b.txt")
    assert called == []


def test_mail_inline_image_becomes_a_cited_child(cached, monkeypatch, capsys):
    _engines(monkeypatch, Fake("vision", ["INVOICE 9"]), Fake("paddle", ["INVOICE 9"]))
    mail = inline_image_mail(
        cached / "mail.eml", card("INVOICE 9", (300, 100)), card("C", (300, 100))
    )
    rows, _ = _read(capsys, mail)
    assert {f"{mail}#att=chart.png", f"{mail}#att=inv.png"} <= set(rows)
    assert "Inline images: inv.png" in Path(rows[str(mail)]["out"]).read_text()
    md = Path(rows[f"{mail}#att=inv.png"]["out"]).read_text()
    assert "@px(30,10,150,18)| INVOICE 9" in md


@needs_ffmpeg
def test_video_gets_transcript_scenes_and_capped_frames(cached, monkeypatch, capsys):
    from tests.fixtures.make_media import scenes_video

    (vision,) = _engines(monkeypatch, Fake("vision", ["SLIDE"]))
    speech = FakeAsr()
    monkeypatch.setattr(asr, "select", lambda spec, s, down=frozenset(): speech)
    video = scenes_video(cached / "clip.mp4")
    rows, _ = _read(capsys, video, "--frames", "2")
    md = Path(rows[str(video)]["out"]).read_text()
    assert "@00:00:00.5-00:00:01.5| 안녕하세요 회의 시작합니다" in md
    assert "scenes: 00:00:00.0, 00:00:02.0, 00:00:04.0" in md
    assert len(vision.calls) == 2
    assert f"## {video}@00:00:00.0-00:00:00.0\n" in md
    assert rows[str(video)]["needs"] == []

    _read(capsys, video, "--frames", "2")
    assert (speech.calls, len(vision.calls)) == (1, 2)


@needs_ffmpeg
def test_speech_language_is_detected_unless_asr_lang_names_one(cached, monkeypatch, capsys):
    import wave

    speech = FakeAsr()
    monkeypatch.setattr(asr, "select", lambda spec, s, down=frozenset(): speech)
    clip = cached / "talk.wav"
    with wave.open(str(clip), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\0\0" * 16000)
    # The top-level lang is the OCR text language and never reaches Whisper
    (cached / "meltify.toml").write_text('lang = "ko"\n')
    _read(capsys, clip)
    assert speech.lang == "auto"
    (cached / "meltify.toml").write_text('lang = "ko"\n[asr]\nlang = "en"\n')
    _read(capsys, clip)
    # A different speech language is a different cache entry, so it transcribes again
    assert (speech.calls, speech.lang) == (2, "en")


def _fake_web(monkeypatch, tmp_path, kind, body=b"", content_type="text/html"):
    page = tmp_path / "dl" / "page.bin"
    page.parent.mkdir(exist_ok=True)
    page.write_bytes(body)
    seen = {}

    @dataclass
    class FetchOptions:
        max_bytes: int = 20_000_000
        allow_private: bool = False
        ignore_robots: bool = False
        refresh: bool = False
        render: bool = False

    @dataclass
    class Fetched:
        url: str
        final_url: str
        kind: str
        path: Path | None
        content_type: str
        fetched_at: str
        sha256: str
        etag: str | None

    def fetch(url, out_dir, opts):
        seen["opts"] = opts
        if "blocked" in url:
            raise ValueError("private address")
        return Fetched(
            url, url + "?final", kind, None if kind == "media" else page, content_type,
            "2026-10-05T00:00:00Z", "ab" * 32, '"e1"',
        )  # fmt: skip

    def convert_page(path, src, *, whole=False):
        from meltify.converters import Block, Converted

        return Converted("web", [Block(replace_anchor(src), f"whole={whole} body")])

    def replace_anchor(src):
        from dataclasses import replace

        return replace(src, anchor="main", line=1)

    fetch_mod = types.ModuleType("meltify.fetch")
    fetch_mod.FetchOptions, fetch_mod.Fetched, fetch_mod.fetch = FetchOptions, Fetched, fetch
    web_mod = types.ModuleType("meltify.converters.web")
    web_mod.convert_page = convert_page
    monkeypatch.setitem(sys.modules, "meltify.fetch", fetch_mod)
    monkeypatch.setitem(sys.modules, "meltify.converters.web", web_mod)
    return seen


def test_url_inputs_route_by_kind_and_record_the_fetch(cached, monkeypatch, capsys):
    seen = _fake_web(monkeypatch, cached, "html")
    url = "https://example.com/post"
    rows, _ = _read(capsys, url, "--whole", "--allow-private", "--max-bytes", "1000")
    row = rows[f"{url}?final"]
    assert (row["final_url"], row["etag"], row["sha256"]) == (f"{url}?final", '"e1"', "ab" * 32)
    assert row["fetched_at"] == "2026-10-05T00:00:00Z"
    md = Path(row["out"]).read_text()
    assert md.startswith(f"<!-- meltify source: {url}?final fetched_at=2026-10-05T00:00:00Z")
    assert f"## {url}?final#main:1\nwhole=True body" in md
    assert (seen["opts"].max_bytes, seen["opts"].allow_private) == (1000, True)

    import pymupdf

    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "a downloaded report with enough text", fontsize=12)
    _fake_web(monkeypatch, cached, "file", doc.tobytes(), "application/pdf")
    rows, _ = _read(capsys, "https://example.com/r.pdf")
    assert rows["https://example.com/r.pdf"]["kind"] == "pdf"
    md = Path(rows["https://example.com/r.pdf"]["out"]).read_text()
    assert "## https://example.com/r.pdf#p1\na downloaded report" in md

    rows, out = _read(capsys, "https://blocked.example/x")
    assert "private address" in rows["https://blocked.example/x"]["error"]


def test_url_media_uses_subtitles_instead_of_speech(cached, monkeypatch, capsys):
    from meltify import video
    from tests.fixtures.make_media import VTT

    _fake_web(monkeypatch, cached, "media")
    speech = FakeAsr()
    monkeypatch.setattr(asr, "select", lambda spec, s, down=frozenset(): speech)

    def download(url, out_dir, langs, subs_only, height, allow_private):
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "v.ko.vtt").write_text(VTT)
        return None, [out_dir / "v.ko.vtt"]

    monkeypatch.setattr(video, "download", download)
    url = "https://youtu.be/abc"
    rows, _ = _read(capsys, url)
    md = Path(rows[url]["out"]).read_text()
    assert f"## {url}\n> from subtitles v.ko.vtt\n@00:00:00.5-00:00:01.8| first words" in md
    assert speech.calls == 0

    rows, _ = _read(capsys, url, "--shallow")
    assert rows[url]["needs"] == ["media"]


def test_missing_url_support_is_a_need(cached, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "meltify.fetch", None)
    rows, _ = _read(capsys, "https://example.com/")
    assert rows["https://example.com/"]["needs"] == ["url support missing"]


def test_pymupdf_converters_share_one_lock(tmp_path):
    import threading

    from meltify.converters import Converted
    from meltify.melt import Reader

    reader = Reader(tmp_path, {})
    assert reader.pdf_lock is run.LOCK
    held = []

    def probe(free: list[bool]) -> None:
        free.append(run.LOCK.acquire(blocking=False))
        if free[0]:
            run.LOCK.release()

    def taken() -> bool:
        # The lock is reentrant, so only another thread can tell whether it is held
        free: list[bool] = []
        t = threading.Thread(target=probe, args=(free,))
        t.start()
        t.join()
        return not free[0]

    def convert(path, src):
        held.append(taken())
        return Converted("pdf")

    # legacy starts soffice before PyMuPDF, so it takes the lock itself only around PyMuPDF
    for kind in ("pdf", "legacy", "text"):
        reader._convert(convert, tmp_path, Src("x"), {"kind": kind})
    assert held == [True, False, False]


def test_shallow_lists_needs_like_01_and_loads_nothing(cached, monkeypatch, capsys):
    called = []
    monkeypatch.setattr(engines, "select", lambda spec, s: called.append(spec) or [])
    monkeypatch.setattr(render, "soffice", lambda: None)
    monkeypatch.setattr(metafile, "replay_available", lambda: False)
    monkeypatch.setattr(quicklook, "enabled", lambda: False)
    pdf = embedded_pdf(cached / "report.pdf")
    docx = embedded_docx(cached / "memo.docx", card("A", (300, 100)))
    # Quick Look stays off too, though this machine may have it
    monkeypatch.setattr(quicklook, "previewable", lambda p: pytest.fail("Quick Look ran"))
    rows, _ = _read(capsys, pdf, docx, "--shallow")
    # Pictures inside documents are counted, not read
    assert rows[str(pdf)]["needs"] == ["ocr pages 3", "ocr"]
    assert "#img" not in Path(rows[str(pdf)]["out"]).read_text()
    docx_needs = [n for n in rows[str(docx)]["needs"] if n != "markitdown"]
    assert docx_needs == [
        "ocr",
        "1 linked image not read",
        f"1 emf image not read ({embeds.DRAW_HINT})",
    ]
    assert called == []


def test_docx_pictures_are_read_without_the_office_extra(cached, monkeypatch, capsys):
    import importlib.util

    real = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda n, *a: None if n == "markitdown" else real(n, *a)
    )
    monkeypatch.setattr(engines, "select", lambda spec, s: [])
    docx = embedded_docx(cached / "memo.docx", card("A", (300, 100)))
    rows, _ = _read(capsys, docx)
    needs = rows[str(docx)]["needs"]
    assert "markitdown" in needs
    # With no engine at hand the picture is still found and listed for OCR
    assert any(n.startswith("ocr") for n in needs)


def test_ffmpeg_failure_on_one_video_leaves_a_need_not_a_crash(cached, monkeypatch):
    from meltify import ffmpeg
    from meltify.converters import RecognizeJob
    from meltify.recognize import Options, Recognizer

    _engines(monkeypatch, Fake("vision", ["frame text"]))
    monkeypatch.setattr(asr, "select", lambda spec, s, down=frozenset(): FakeAsr())
    monkeypatch.setattr(ffmpeg, "has_audio", lambda path: pytest.fail("frames are tried first"))

    def refuse(path, out_dir, threshold, window):
        raise RuntimeError("ffmpeg exited 234: no decoder found for: svg")

    monkeypatch.setattr(ffmpeg, "scene_frames", refuse)
    clip = cached / "pic.qqq"
    clip.write_bytes(b"not decodable")
    image = cached / "a.png"
    image.write_bytes(card("STILL READ", (300, 100)))
    warned = []
    rec = Recognizer(Options(), config.defaults(), warned.append)
    jobs = [
        RecognizeJob("video", Src("pic.qqq"), path=clip, listen=False),
        RecognizeJob("image", Src("a.png"), path=image),
    ]
    # Already transcribed, so only the frames are needed and ffmpeg fails on them
    video, still = rec.run(jobs)
    assert video.left == "failed" and video.blocks == []
    assert still.left is None and "frame text" in still.blocks[0].text
    assert warned == ["can't read pic.qqq: ffmpeg exited 234: no decoder found for: svg"]


def test_rendered_pages_key_on_their_source_not_the_render(tmp_path, monkeypatch):
    from meltify.converters import RecognizeJob
    from meltify.recognize import Options, Recognizer

    source = tmp_path / "memo.wps"
    source.write_bytes(b"works memo")
    made = iter(range(2))

    def convert_to(path, target, out_dir, timeout):
        # soffice stamps every PDF with the time it was made
        out_dir.mkdir(parents=True, exist_ok=True)
        pdf = out_dir / f"{path.stem}.pdf"
        pdf.write_bytes(f"%PDF-1.7 CreationDate {next(made)}".encode())
        return pdf

    monkeypatch.setattr(render, "soffice", lambda: "/opt/soffice")
    monkeypatch.setattr(render, "convert_to", convert_to)
    pdfs = [render.to_pdf(source, out_dir=tmp_path / f"r{n}") for n in range(2)]
    assert pdfs[0].read_bytes() != pdfs[1].read_bytes()
    reader = Recognizer(Options(), config.defaults(), print)
    keys = {reader.key(RecognizeJob("page", Src(str(source), page=1), path=p)) for p in pdfs}
    assert len(keys) == 1


@dataclass
class Slow(Fake):
    """A fake that takes a while and counts how many of its calls overlap"""

    delay: float = 0.2
    running: int = 0
    peak: int = 0

    def recognize(self, image, size):
        import threading
        import time

        lock = self.__dict__.setdefault("_lock", threading.Lock())
        with lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
        time.sleep(self.delay)
        with lock:
            self.running -= 1
        return super().recognize(image, size)


def _cards(folder, n):
    folder.mkdir()
    for i in range(n):
        (folder / f"{i}.png").write_bytes(card(f"CARD {i}", (300, 100)))
    return folder


def test_uncached_pictures_are_read_side_by_side_in_job_order(cached, monkeypatch, capsys):
    from meltify import recognize

    vision, served = _engines(
        monkeypatch, Slow("vision", ["SAME"]), Slow("served", ["SAME"], delay=0.05)
    )
    folder = _cards(cached / "in", 6)
    rows, _ = _read(capsys, folder)
    # Every OCR engine builds its own request, so none of them waits on a lock
    assert vision.peak > 1 and served.peak > 1
    parallel = {c: Path(r["out"]).read_text() for c, r in rows.items()}

    monkeypatch.setattr(recognize, "WORKERS", 1)
    rows, _ = _read(capsys, folder, "--refresh")
    assert {c: Path(r["out"]).read_text() for c, r in rows.items()} == parallel


def test_worker_warnings_come_out_in_job_order(cached, monkeypatch, capsys):
    import time

    class Failing(Fake):
        def recognize(self, image, size):
            # Later jobs finish first, so only the reordering keeps the warnings in order
            n = int(Image.open(image).width)
            time.sleep(max(0, 2000 - n) / 4000)
            raise RuntimeError(f"broke on width {n}")

    from PIL import Image

    _engines(monkeypatch, Failing("vision", []))
    folder = cached / "in"
    folder.mkdir()
    for i in range(5):
        (folder / f"{i}.png").write_bytes(card(f"W{i}", (300 + 40 * i, 100)))
    _, out = _read(capsys, folder)
    widths = [int(re.search(r"width (\d+)", w).group(1)) for w in out["warnings"] if "broke" in w]
    assert widths == sorted(widths) and len(set(widths)) == 5


def test_big_pictures_wait_for_ocr_on_disk_with_the_same_cache_key(tmp_path, monkeypatch):
    from meltify.converters import Converted, RecognizeJob
    from meltify.melt import PICTURE_SPILL_BYTES, Reader
    from meltify.recognize import Options, Recognizer

    temps = run.Temps()
    run.use(run.RunContext(temps=temps))
    try:
        big = card("BIG", (2000, 1500)) + bytes(PICTURE_SPILL_BYTES)
        small = card("SMALL", (200, 80))
        reader = Reader(tmp_path / "out", {})
        jobs = [
            RecognizeJob("image", Src("a.docx", img=n), data=d) for n, d in ((1, big), (2, small))
        ]
        reader.keep(Converted("office", jobs=jobs), "a.docx", 0, {}, "a.docx")
        held = reader.outputs[0].converted.jobs
        assert held[0].data is None and held[0].path.read_bytes() == big
        assert held[1].data == small
        rec = Recognizer(Options(), config.defaults(), lambda m: None)
        assert [rec.key(j) for j in held] == [rec.key(j) for j in jobs]
        spill = held[0].path.parent
    finally:
        temps.remove()
        run.use(None)
    assert not spill.exists()


def test_an_item_engines_read_without_finding_text_says_so(cached, monkeypatch, capsys):
    from PIL import Image

    Image.new("RGB", (200, 120), "white").save(cached / "blank.png")
    (cached / "words.png").write_bytes(card("WORDS 12", (300, 100)))
    _engines(monkeypatch, Fake("vision", []))
    rows, _ = _read(capsys, cached / "blank.png")
    assert rows[str(cached / "blank.png")]["needs"] == ["no text found"]

    _engines(monkeypatch, Fake("vision", ["WORDS 12"]))
    rows, _ = _read(capsys, cached / "words.png")
    assert rows[str(cached / "words.png")]["needs"] == []


def test_lang_flag_picks_the_ocr_language_and_its_own_cache_entry(cached, monkeypatch, capsys):
    import meltify

    seen = []
    fake = Fake("vision", ["WORDS 12"])

    def select(spec, settings):
        seen.append(settings["lang"])
        return [fake]

    monkeypatch.setattr(engines, "select", select)
    (cached / "words.png").write_bytes(card("WORDS 12", (300, 100)))
    _read(capsys, cached / "words.png")
    _read(capsys, cached / "words.png", "--lang", "en")
    meltify.read(cached / "words.png", lang="en")
    assert seen == ["ko", "en", "en"]
    # The second language read the picture again instead of reusing the Korean reading
    assert len(fake.calls) == 2


def test_an_engine_named_on_the_command_line_is_checked_before_anything_is_written(
    cached, monkeypatch, capsys
):
    # Only the key check runs, so no paid API is ever called
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    (cached / "a.txt").write_text("alpha\n")
    assert main(["read", "a.txt", "--engines", "gemini", "--json"]) == 3
    assert json.loads(capsys.readouterr().out)["errors"][0]["code"] == "missing"
    assert not list((cached / "meltify-out").rglob("*.md"))


def test_an_engine_named_only_in_config_leaves_pictures_listed(cached, monkeypatch, capsys):
    monkeypatch.setenv("MELTIFY_OCR_ENGINES", "gemini")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    (cached / "a.png").write_bytes(card("TOTAL", (300, 100)))
    (cached / "b.txt").write_text("plain")
    assert main(["read", "a.png", "b.txt", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    rows = {r["cite"]: r for r in out["results"]}
    assert rows["a.png"]["needs"] == ["ocr"]
    assert any("images stay listed as needs" in w for w in out["warnings"])
    # Named on the command line, the same engine fails the run before anything is written
    assert main(["read", "a.png", "--engines", "gemini", "--json"]) == 3
