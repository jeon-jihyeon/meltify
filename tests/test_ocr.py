import importlib.util
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from PIL import Image, ImageDraw

from meltify import config, imaging
from meltify.cli import main
from meltify.engines import ocr as engines
from meltify.engines.consensus import EngineReading, compare
from meltify.engines.ocr import LOCAL, REMOTE, TextBox
from meltify.safe import Unreachable


@dataclass
class Fake:
    name: str
    lines: list[str]
    kind: str = LOCAL

    def missing(self):
        return None

    def recognize(self, image, size):
        return [TextBox(t, (10, 20 * i, 100, 20 * i + 15), 0.9) for i, t in enumerate(self.lines)]


def test_consensus_needs_two_engines_and_a_local_one():
    a = EngineReading("vision", LOCAL, [TextBox("김치찌개 655 kcal"), TextBox("총 1,250")])
    b = EngineReading("paddle", LOCAL, [TextBox("김치찌개 665 kcal"), TextBox("총 １,250")])
    got = {v.value: v.agreed for v in compare([a, b])}
    assert got == {"655": False, "665": False, "1,250": True}

    c = EngineReading("claude", REMOTE, [TextBox("655")])
    d = EngineReading("gemini", REMOTE, [TextBox("655")])
    assert [v.agreed for v in compare([c, d])] == [False]
    assert [v.agreed for v in compare([a])] == [False, False]


def test_token_mode_compares_words():
    a = EngineReading("a", LOCAL, [TextBox("STOP here")])
    b = EngineReading("b", LOCAL, [TextBox("ST0P here")])
    got = {v.value: v.agreed for v in compare([a, b], "tokens")}
    assert got["here"] is True and got["STOP"] is False and got["ST0P"] is False


def test_each_value_cites_the_first_box_with_a_bbox_in_engine_order():
    a = EngineReading("a", LOCAL, [TextBox("1,250 and 7"), TextBox("１,250", (1, 1, 2, 2))])
    b = EngineReading(
        "b", LOCAL, [TextBox("7 then 9", (5, 5, 6, 6)), TextBox("1,250", (3, 3, 4, 4))]
    )
    got = {v.value: v.bbox for v in compare([a, b])}
    assert got == {"1,250": (1, 1, 2, 2), "7": (5, 5, 6, 6), "9": (5, 5, 6, 6)}
    assert [v.bbox for v in compare([EngineReading("c", LOCAL, [TextBox("42")])])] == [None]


def test_upscale_and_equalize_shape_the_picture_and_its_cache_key(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    seen = []

    @dataclass
    class Sizer(Fake):
        def recognize(self, image, size):
            seen.append(size)
            return super().recognize(image, size)

    monkeypatch.setattr(engines, "select", lambda spec, s: [Sizer("vision", ["TOTAL 655"])])
    menu = str(_menu(tmp_path / "m.png"))
    assert main(["read", menu, "--upscale", "2", "--no-sharpen", "--equalize"]) == 0
    assert seen == [(600, 160)]
    # Another preparation is another reading, not a cache hit
    assert main(["read", menu, "--upscale", "3"]) == 0
    assert seen == [(600, 160), (900, 240)]
    assert main(["read", menu, "--upscale", "3"]) == 0
    assert len(seen) == 2


def test_tiles_cover_large_images():
    big = Image.new("RGB", (6000, 1000))
    ts = imaging.tiles(big, 2576)
    assert all(max(t.image.size) <= 2576 for t in ts)
    assert max(t.x + t.image.width for t in ts) == 6000
    assert imaging.tiles(Image.new("RGB", (100, 100)), 2576)[0].x == 0


def _scans(path: Path, pages: int) -> Path:
    import io

    import pymupdf

    doc = pymupdf.open()
    for n in range(pages):
        buf = io.BytesIO()
        img = Image.new("RGB", (400, 200), "white")
        ImageDraw.Draw(img).text((10, 30), f"PAGE {n}", fill="black")
        img.save(buf, format="PNG")
        page = doc.new_page()
        page.insert_image(page.rect, stream=buf.getvalue())
    doc.save(path)
    return path


def _menu(path: Path) -> Path:
    img = Image.new("RGB", (300, 80), "white")
    ImageDraw.Draw(img).text((10, 30), "TOTAL 655", fill="black")
    img.save(path)
    return path


def _rows(capsys):
    out = json.loads(capsys.readouterr().out)
    return out, [r for r in out["results"] if r["kind"] == "disputed"]


def test_read_lists_values_the_engines_dispute(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        engines,
        "select",
        lambda spec, s: [Fake("vision", ["TOTAL 655"]), Fake("paddle", ["TOTAL 665"])],
    )
    assert main(["read", str(_menu(tmp_path / "m.png")), "--json"]) == 0
    out, disputed = _rows(capsys)
    assert [r["value"] for r in disputed] == ["655", "665"]
    assert disputed[0]["cite"].startswith(f"{tmp_path}/m.png@px(")
    assert "2 disputed values" in out["summary"]


def test_compare_tokens_disputes_words(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        engines,
        "select",
        lambda spec, s: [Fake("vision", ["STOP here"]), Fake("paddle", ["SHOP here"])],
    )
    menu = str(_menu(tmp_path / "m.png"))
    assert main(["read", menu, "--json"]) == 0
    assert _rows(capsys)[1] == []
    assert main(["read", menu, "--compare", "tokens", "--json"]) == 0
    assert {r["value"] for r in _rows(capsys)[1]} == {"STOP", "SHOP"}


def test_agent_reading_counts_as_an_engine(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(engines, "select", lambda spec, s: [Fake("vision", ["TOTAL 655"])])
    (tmp_path / "mine.txt").write_text("TOTAL 665\n")
    menu = str(_menu(tmp_path / "m.png"))
    assert main(["read", menu, "--reading", "agent=mine.txt", "--json"]) == 0
    out, disputed = _rows(capsys)
    counts = {r["value"]: r["counts"] for r in disputed}
    assert counts == {"655": {"vision": 1, "agent": 0}, "665": {"vision": 0, "agent": 1}}
    # The reading isn't cached, so a run without it has one engine again
    assert main(["read", menu, "--json"]) == 0
    assert _rows(capsys)[1] == []


def test_a_reading_alone_needs_no_engine(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(engines, "select", lambda spec, s: [])
    (tmp_path / "mine.txt").write_text("TOTAL 655\n")
    assert main(["read", str(_menu(tmp_path / "m.png")), "--reading", "me=mine.txt"]) == 0
    md = (tmp_path / "meltify-out/read/m.png.md").read_text()
    assert "TOTAL 655" in md and "unchecked: one engine" in md


class FakeRemote(engines.Remote):
    """Read the same line on every tile, like an LLM does for a line in the overlap"""

    def __init__(self, lines: list[str], fail: Exception | None = None) -> None:
        super().__init__("gemini", "m", "FAKE_KEY")
        self.lines = lines
        self.fail = fail

    def missing(self):
        return None

    def recognize(self, image, size):
        if self.fail is not None:
            raise self.fail
        return [TextBox(t) for t in self.lines]


def test_remote_lines_in_tile_overlap_count_once(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        engines,
        "select",
        lambda spec, s: [Fake("vision", ["TOTAL 655"]), FakeRemote(["TOTAL 655"])],
    )
    tiles = []
    real = engines.read_tiles
    monkeypatch.setattr(
        engines,
        "read_tiles",
        lambda e, image, size, work: tiles.append(1) or real(e, image, size, work),
    )
    original = imaging.tiles
    monkeypatch.setattr(imaging, "tiles", lambda *a, **k: tiles.extend(t := original(*a, **k)) or t)
    assert main(["read", str(_menu(tmp_path / "m.png")), "--upscale", "10", "--json"]) == 0
    out, disputed = _rows(capsys)
    assert len(tiles) == 3 and disputed == []
    assert "0 disputed values" in out["summary"]


def test_failing_remote_engine_warns_and_others_go_on(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FAKE_KEY", "secret-value")
    monkeypatch.setattr(
        engines,
        "select",
        lambda spec, s: [
            Fake("vision", ["TOTAL 655"]),
            Fake("paddle", ["TOTAL 655"]),
            FakeRemote([], KeyError("candidates")),
        ],
    )
    assert main(["read", str(_menu(tmp_path / "m.png")), "--json"]) == 0
    out, disputed = _rows(capsys)
    assert any(w.startswith("gemini failed on") for w in out["warnings"])
    assert disputed == []
    assert "secret-value" not in json.dumps(out)


def test_reading_needs_one_target_and_the_name_file_form(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(engines, "select", lambda spec, s: [Fake("vision", ["TOTAL 655"])])
    (tmp_path / "mine.txt").write_text("TOTAL 655\n")
    a, b = _menu(tmp_path / "a.png"), _menu(tmp_path / "b.png")
    assert main(["read", str(a), str(b), "--reading", "agent=mine.txt", "--json"]) == 2
    assert "exactly one local image" in capsys.readouterr().out
    pdf = tmp_path / "two.pdf"
    _scans(pdf, 2)
    assert main(["read", str(pdf), "--reading", "agent=mine.txt", "--json"]) == 2
    assert "exactly one image or OCR'd PDF page, got 2" in capsys.readouterr().out
    assert main(["read", str(pdf), "--pages", "2", "--reading", "agent=mine.txt"]) == 0
    capsys.readouterr()
    assert main(["read", str(a), "--reading", str(tmp_path), "--json"]) == 2
    assert "NAME=FILE" in capsys.readouterr().out


def test_no_engine_leaves_the_picture_listed(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(engines, "select", lambda spec, s: [])
    assert main(["read", str(_menu(tmp_path / "m.png")), "--json"]) == 0
    [row] = json.loads(capsys.readouterr().out)["results"]
    assert row["needs"] == ["ocr"]


def test_paid_engines_are_never_auto():
    names = [e.name for e in engines.select("auto", config.defaults())]
    assert not {"claude", "gemini", "openai"} & set(names)


def _served(base_url: str, **table) -> dict:
    settings = config.defaults()
    settings["ocr"]["endpoints"] = {"vl": {"base_url": base_url, "model": "ocr-vl", **table}}
    return settings


def test_auto_runs_an_endpoint_you_serve_but_counts_it_as_an_llm(monkeypatch):
    settings = _served("http://127.0.0.1:8111/v1")
    vl = next(e for e in engines.select("auto", settings) if e.name == "vl")
    assert vl.missing() is None
    assert "vl" in engines.names(settings)
    # A served VLM can guess like any LLM, so it can't make a value agreed with another LLM
    readings = [
        EngineReading("vl", vl.kind, [TextBox("합계 9,999")]),
        EngineReading("claude", REMOTE, [TextBox("합계 9,999")]),
    ]
    assert not compare(readings)[0].agreed

    settings["ocr"]["endpoints"]["vl"]["key_env"] = "VL_KEY"
    monkeypatch.delenv("VL_KEY", raising=False)
    assert engines.build("vl", settings).missing() == "export VL_KEY=..."


def test_a_hosted_api_never_counts_as_an_endpoint():
    assert "loopback or private" in engines.build("vl", _served("https://8.8.8.8/v1")).missing()
    assert "vl" not in [e.name for e in engines.select("auto", _served("https://8.8.8.8/v1"))]


@pytest.mark.parametrize(
    ("table", "message"),
    [
        ({"base_url": "http://127.0.0.1/v1"}, "needs base_url and model"),
        ({"base_url": "http://127.0.0.1/v1", "model": "m", "url": "x"}, "unknown field url"),
        ({"base_url": "http://127.0.0.1/v1", "model": "m", "timeout": "5"}, "wrong type"),
    ],
)
def test_endpoint_tables_fail_loudly(table, message):
    settings = config.defaults()
    settings["ocr"]["endpoints"] = {"vl": table}
    with pytest.raises(ValueError, match=message):
        engines.select("auto", settings)
    settings["ocr"]["endpoints"] = {"vision": {"base_url": "http://127.0.0.1/v1", "model": "m"}}
    with pytest.raises(ValueError, match="built-in engine"):
        engines.select("auto", settings)


def test_an_endpoint_sends_its_prompt_and_timeout_and_no_key_by_default(monkeypatch, tmp_path):
    seen = {}

    def post(url, json, headers, timeout):
        seen.update(
            url=url,
            headers=headers,
            timeout=timeout,
            prompt=json["messages"][0]["content"][0]["text"],
        )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "합계 1,250원"}}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", post)
    vl = engines.build("vl", _served("http://localhost:8111/v1", timeout=300, prompt="OCR:"))
    image = tmp_path / "a.png"
    image.write_bytes(b"png")
    assert [b.text for b in vl.recognize(image, (1, 1))] == ["합계 1,250원"]
    assert seen == {
        "url": "http://localhost:8111/v1/chat/completions",
        "headers": {},
        "timeout": 300,
        "prompt": "OCR:",
    }


def test_a_server_that_is_down_is_unreachable(monkeypatch, tmp_path):
    def post(url, **kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "post", post)
    image = tmp_path / "a.png"
    image.write_bytes(b"png")
    with pytest.raises(Unreachable, match="nothing answers"):
        engines.build("vl", _served("http://127.0.0.1:9/v1")).recognize(image, (1, 1))


@pytest.mark.macos
@pytest.mark.skipif(
    sys.platform != "darwin" or importlib.util.find_spec("ocrmac") is None, reason="needs Vision"
)
def test_vision_reads_korean_menu(tmp_path):
    from PIL import ImageFont

    font = ImageFont.truetype("/System/Library/Fonts/AppleSDGothicNeo.ttc", 28)
    img = Image.new("RGB", (420, 120), "white")
    d = ImageDraw.Draw(img)
    d.text((10, 10), "김치찌개 655kcal", font=font, fill="black")
    d.text((10, 60), "제육볶음 820kcal", font=font, fill="black")
    p = tmp_path / "menu.png"
    img.save(p)
    boxes = engines.Vision("ko").recognize(p, img.size)
    text = " ".join(b.text for b in boxes)
    assert "655" in text and "820" in text
    assert all(b.bbox and 0 <= b.bbox[0] < b.bbox[2] <= 420 for b in boxes)


def test_lang_flag_reaches_the_engines(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    seen = []
    monkeypatch.setattr(
        engines, "select", lambda spec, s: seen.append(s["lang"]) or [Fake("vision", ["x"])]
    )
    assert main(["read", str(_menu(tmp_path / "m.png")), "--lang", "de"]) == 0
    assert seen == ["de"]


def test_lang_flag_is_lowercased_and_checked(tmp_path, monkeypatch, capsys):
    from meltify import lang

    assert lang.code(" EN ") == "en"
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as e:
        main(["read", "m.png", "--lang", "english"])
    assert e.value.code == 2
    assert "unknown language 'english', use one of ko, en" in capsys.readouterr().err


def test_a_library_value_error_keeps_its_name(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(engines, "select", lambda spec, s: [Fake("vision", ["x"])])
    (tmp_path / "mine.txt").write_bytes(b"\xff\xfe bad utf-8 \xc3")
    assert (
        main(["read", str(_menu(tmp_path / "m.png")), "--reading", "agent=mine.txt", "--json"]) == 2
    )
    assert json.loads(capsys.readouterr().out)["errors"][0]["message"].startswith(
        "UnicodeDecodeError: "
    )


def _text_page_over_scan(path: Path) -> Path:
    """A page whose text layer says 48,280 over a picture that shows 48,250"""
    import io

    import pymupdf

    buf = io.BytesIO()
    img = Image.new("RGB", (400, 200), "white")
    ImageDraw.Draw(img).text((10, 30), "TOTAL 48,250", fill="black")
    img.save(buf, format="PNG")
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "TOTAL 48,280 as the layer says, long enough to count as text")
    page.insert_image(pymupdf.Rect(72, 100, 472, 300), stream=buf.getvalue())
    doc.save(path)
    return path


def test_ocr_pages_checks_a_text_layer_against_the_pixels(tmp_path, monkeypatch, capsys):
    import pymupdf

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(engines, "select", lambda spec, s: [Fake("vision", ["TOTAL 48,250"])])
    pdf = str(_text_page_over_scan(tmp_path / "layer.pdf"))
    (tmp_path / "mine.txt").write_text("TOTAL 48,250\n")
    text_only = tmp_path / "text.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "TOTAL 48,280 as the layer says, long enough to count")
    doc.save(text_only)
    # Without --ocr-pages a page with a text layer goes unread, so a reading has no picture
    assert main(["read", str(text_only), "--reading", "agent=mine.txt", "--json"]) == 2
    assert "Add --ocr-pages" in capsys.readouterr().out
    assert main(["read", pdf, "--ocr-pages", "--reading", "agent=mine.txt", "--json"]) == 0
    capsys.readouterr()
    md = (tmp_path / "meltify-out/read/layer.pdf.md").read_text()
    assert "48,280 as the layer says" in md and "@pt(" in md and "48,250" in md


def test_one_engine_is_called_out_in_the_summary(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(engines, "select", lambda spec, s: [Fake("vision", ["TOTAL 655"])])
    assert main(["read", str(_menu(tmp_path / "m.png")), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert "1 pictures read by one engine only" in out["summary"]
    assert any("nothing there was cross-checked" in w for w in out["warnings"])


def test_a_refused_reading_writes_no_markdown(tmp_path, monkeypatch, capsys):
    import pymupdf

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(engines, "select", lambda spec, s: [Fake("vision", ["x"])])
    (tmp_path / "mine.txt").write_text("x\n")
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "a text layer long enough to count as text")
    doc.save(tmp_path / "t.pdf")
    assert main(["read", "t.pdf", "--reading", "agent=mine.txt", "--json"]) == 2
    assert not list((tmp_path / "meltify-out").rglob("*.md"))
