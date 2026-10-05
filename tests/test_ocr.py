import importlib.util
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from meltify import imaging
from meltify.cli import main
from meltify.commands.ocr import Target
from meltify.engines import ocr as engines
from meltify.engines.consensus import EngineReading, compare
from meltify.engines.ocr import LOCAL, REMOTE, TextBox


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


def test_target_maps_back_to_original_units():
    img = Target(None, "menu.png", None, 3, 1 / 3, "px")
    assert img.src((30, 60, 90, 120)).cite() == "menu.png@px(10,20,30,40)"
    page = Target(None, "a.pdf", 2, 1, 72 / 300, "pt")
    assert page.src((300, 300, 600, 600)).cite() == "a.pdf#p2@pt(72,72,144,144)"
    assert page.src(None).cite() == "a.pdf#p2"


def test_pdf_pages_use_dpi_without_upscale(tmp_path):
    import pymupdf

    from meltify.commands.ocr import _targets

    pdf = tmp_path / "a.pdf"
    doc = pymupdf.open()
    doc.new_page(width=72, height=144)
    doc.save(pdf)
    (page,) = _targets(pdf, None, 300, 3)
    assert (page.upscale, page.image.size) == (1.0, (300, 600))
    (img,) = _targets(_menu(tmp_path / "m.png"), None, 300, 3)
    assert img.upscale == 3


def test_tiles_cover_large_images():
    big = Image.new("RGB", (6000, 1000))
    ts = imaging.tiles(big, 2576)
    assert all(max(t.image.size) <= 2576 for t in ts)
    assert max(t.x + t.image.width for t in ts) == 6000
    assert imaging.tiles(Image.new("RGB", (100, 100)), 2576)[0].x == 0


def _menu(path: Path) -> Path:
    img = Image.new("RGB", (300, 80), "white")
    ImageDraw.Draw(img).text((10, 30), "TOTAL 655", fill="black")
    img.save(path)
    return path


def test_cli_with_fake_engines_lists_disputes_first(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        engines,
        "select",
        lambda spec, s: [Fake("vision", ["TOTAL 655"]), Fake("paddle", ["TOTAL 665"])],
    )
    code = main(["ocr", str(_menu(tmp_path / "m.png")), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert [r["type"] for r in out["results"][:2]] == ["disputed", "disputed"]
    assert out["results"][0]["cite"].startswith(f"{tmp_path}/m.png@px(")
    assert "0 agreed, 2 disputed" in out["summary"]
    assert Path(out["artifacts"][0]["path"]).is_file()


def test_agent_reading_counts_as_an_engine(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(engines, "select", lambda spec, s: [Fake("vision", ["TOTAL 655"])])
    (tmp_path / "mine.txt").write_text("TOTAL 655\n")
    main(
        ["ocr", str(_menu(tmp_path / "m.png")), "--reading", f"agent={tmp_path}/mine.txt", "--json"]
    )
    out = json.loads(capsys.readouterr().out)
    assert "1 agreed, 0 disputed" in out["summary"]


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
    main(["ocr", str(_menu(tmp_path / "m.png")), "--upscale", "10", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert len(list((tmp_path / "meltify-out/ocr/m.png").glob("tile*.png"))) == 2
    assert "1 agreed, 0 disputed" in out["summary"]


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
    assert main(["ocr", str(_menu(tmp_path / "m.png")), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert any(w.startswith("gemini failed on") for w in out["warnings"])
    assert "1 agreed, 0 disputed" in out["summary"]
    assert "secret-value" not in json.dumps(out)


def test_reading_needs_one_target_and_the_name_file_form(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(engines, "select", lambda spec, s: [Fake("vision", ["TOTAL 655"])])
    (tmp_path / "mine.txt").write_text("TOTAL 655\n")
    a, b = _menu(tmp_path / "a.png"), _menu(tmp_path / "b.png")
    assert main(["ocr", str(a), str(b), "--reading", "agent=mine.txt"]) == 2
    assert "exactly one image" in capsys.readouterr().err
    assert main(["ocr", str(a), "--reading", str(tmp_path)]) == 2
    assert "NAME=FILE" in capsys.readouterr().err


def test_no_engine_exits_three(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(engines, "select", lambda spec, s: [])
    assert main(["ocr", str(_menu(tmp_path / "m.png"))]) == 3


def test_paid_engines_are_never_auto():
    names = [e.name for e in engines.select("auto", {"lang": "ko", "llm": {}})]
    assert not {"claude", "gemini", "openai"} & set(names)


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
