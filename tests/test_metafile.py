import gzip
import struct

import pytest

from meltify.converters import metafile, render
from meltify.converters.embeds import DRAW_HINT, Embeds
from meltify.converters.run import RunContext
from meltify.evidence import Src
from tests.fixtures.make_binary import pad as _pad
from tests.fixtures.make_binary import wmf, wmf_textout, wmr
from tests.fixtures.make_docs import card

HANGUL = 129
SHIFTJIS = 128


def emr(kind: int, body: bytes) -> bytes:
    body = _pad(body, 4)
    return struct.pack("<II", kind, 8 + len(body)) + body


def emf(*records: bytes) -> bytes:
    """A minimal EMF: the header, the given records, then EOF"""
    eof = emr(14, struct.pack("<III", 0, 16, 20))
    size = 88 + sum(map(len, records)) + len(eof)
    head = (
        struct.pack("<iiii", 0, 0, 400, 100)  # bounds in device pixels
        + struct.pack("<iiii", 0, 0, 10583, 2645)  # frame in 0.01 mm
        + b" EMF"
        + struct.pack("<IIIHHIII", 0x10000, size, len(records) + 2, 4, 0, 0, 0, 0)
        + struct.pack("<iiii", 1024, 768, 270, 203)
    )
    return emr(1, head) + b"".join(records) + eof


def font(index: int, height: int, charset: int = 1) -> bytes:
    logfont = struct.pack("<iiiii", height, 0, 0, 0, 400) + bytes([0, 0, 0, charset, 0, 0, 0, 0])
    return emr(82, struct.pack("<I", index) + logfont + bytes(64))


def select(index: int) -> bytes:
    return emr(37, struct.pack("<I", index))


def text_w(x: int, y: int, text: str, advance: list[int] | None = None, options: int = 0) -> bytes:
    raw = _pad(text.encode("utf-16-le"), 4)
    dx = b"" if advance is None else struct.pack(f"<{len(advance)}i", *advance)
    head = bytes(16) + struct.pack("<Iff", 1, 1.0, 1.0)
    off_dx = 76 + len(raw) if dx else 0
    emrtext = struct.pack("<iiIII", x, y, len(text), 76, options) + bytes(16)
    return emr(84, head + emrtext + struct.pack("<I", off_dx) + raw + dx)


def text_a(x: int, y: int, raw: bytes) -> bytes:
    head = bytes(16) + struct.pack("<Iff", 1, 1.0, 1.0)
    emrtext = struct.pack("<iiIII", x, y, len(raw), 76, 0) + bytes(16)
    return emr(83, head + emrtext + struct.pack("<I", 0) + _pad(raw, 4))


def stretch_dibits() -> bytes:
    return emr(81, bytes(72))


def plus(*records: bytes) -> bytes:
    data = b"EMF+" + b"".join(records)
    return emr(70, struct.pack("<I", len(data)) + data)


def plus_record(kind: int, flags: int, data: bytes) -> bytes:
    data = _pad(data, 4)
    return struct.pack("<HHII", kind, flags, 12 + len(data), len(data)) + data


def plus_string(x: float, y: float, text: str) -> bytes:
    raw = text.encode("utf-16-le")
    body = struct.pack("<III", 0, 0, len(text)) + struct.pack("<ffff", x, y, 200, 40) + raw
    return plus_record(0x401C, 0x8000, body)


def wmf_font(height: int, charset: int) -> bytes:
    return wmr(
        0x02FB, struct.pack("<hhhhh", height, 0, 0, 0, 400) + bytes([0, 0, 0, charset]) + bytes(36)
    )


def wmf_exttextout(x: int, y: int, raw: bytes, options: int = 0) -> bytes:
    return wmr(0x0A32, struct.pack("<hhHH", y, x, len(raw), options) + _pad(raw, 2))


def glyph_by_glyph(x: int, y: int, text: str, step: int = 10) -> list[bytes]:
    return [text_w(x + i * step, y, c, [step]) for i, c in enumerate(text)]


def test_glyph_at_a_time_text_is_joined_into_lines():
    data = emf(
        font(1, 20),
        select(1),
        *glyph_by_glyph(100, 50, "Revenue"),
        # No space record between the words, only a gap wider than a space
        *glyph_by_glyph(185, 50, "2026"),
        text_w(100, 80, "Total 4,210", [10] * 11),
    )
    found = metafile.scan(data)
    assert found.kind == "emf"
    assert [(line.text, line.x, line.y) for line in found.lines] == [
        ("Revenue 2026", 100, 50),
        ("Total 4,210", 100, 80),
    ]
    assert (found.bitmaps, found.undecoded) == (0, {})


def test_glyph_index_text_is_listed_and_bitmaps_counted():
    data = emf(text_w(10, 10, "Kept"), text_w(10, 40, "\x01\x02", options=0x10), stretch_dibits())
    found = metafile.scan(data)
    assert found.text == "Kept"
    assert found.undecoded == {"glyph-index text": 1}
    assert found.bitmaps == 1


def test_ansi_text_decodes_with_the_selected_font_charset():
    data = emf(font(3, 24, HANGUL), select(3), text_a(0, 0, "매출 합계".encode("cp949")))
    assert metafile.scan(data).text == "매출 합계"


def test_dual_emf_plus_keeps_one_copy_of_its_text():
    header = plus_record(0x4001, 0x0001, struct.pack("<IIII", 0xDBC01002, 1, 96, 96))
    data = emf(
        plus(header, plus_string(10, 10, "Quarterly sales\nby region")),
        # The GDI copy for readers without EMF+
        text_w(10, 10, "Quarterly sales"),
        text_w(10, 30, "by region"),
    )
    assert metafile.scan(data).text == "Quarterly sales\nby region"


def test_wmf_decodes_with_its_font_slot_and_charset():
    data = wmf(
        wmr(0x02FA, bytes(10)),  # a pen takes slot 0
        wmf_font(-16, SHIFTJIS),  # the font takes slot 1
        wmr(0x012D, struct.pack("<H", 1)),
        wmr(0x01F0, struct.pack("<H", 0)),
        wmr(0x02FC, bytes(8)),  # a brush reuses slot 0, so the font stays in slot 1
        wmf_exttextout(26, 74, "２００５年度".encode("cp932")),
        wmf_textout(55, 113, b"H17"),
        wmr(0x0F43, bytes(40)),  # STRETCHDIB with a bitmap
    )
    found = metafile.scan(data)
    assert found.kind == "wmf"
    assert [(line.text, line.x, line.y) for line in found.lines] == [
        ("２００５年度", 26, 74),
        ("H17", 55, 113),
    ]
    assert found.bitmaps == 1


def test_placeable_wmf_and_symbol_text():
    data = wmf(wmf_font(12, 2), wmr(0x012D, b"\x00\x00"), wmf_textout(0, 0, b"abc"), placeable=True)
    found = metafile.scan(data)
    assert found.lines == []
    assert found.undecoded == {"symbol font text": 1}


def test_bytes_that_are_no_metafile():
    assert metafile.scan(b"\x01\x00\x00\x00 not really emf") is None
    assert metafile.scan(b"%PDF-1.7") is None
    # A cut-off file still gives the records before the cut
    data = emf(text_w(0, 0, "Before the cut"), text_w(0, 20, "Cut off"))
    assert metafile.scan(data[:-30]).text == "Before the cut"


@pytest.fixture
def no_renderer(monkeypatch):
    monkeypatch.setattr(render, "available", lambda: False)
    monkeypatch.setattr(metafile, "replay_available", lambda: False)


def test_text_only_picture_is_a_cited_block_without_ocr(no_renderer, monkeypatch):
    monkeypatch.setattr(render, "available", lambda: pytest.fail("no render needed"))
    embeds = Embeds()
    embeds.picture(Src("a.docx", para=2, img=1), emf(text_w(0, 0, "Org chart")), "media/a.emf")
    embeds.draw()
    assert [(b.src.cite(), b.text) for b in embeds.blocks] == [("a.docx#para2#img1", "Org chart")]
    assert embeds.jobs == []
    assert embeds.needs() == []


def test_text_with_bitmaps_keeps_the_text_and_lists_the_rest(no_renderer):
    embeds = Embeds()
    embeds.picture(Src("a.docx", img=1), emf(text_w(0, 0, "Logo"), stretch_dibits()), "a.emf")
    embeds.picture(Src("a.docx", img=2), wmf(wmf_exttextout(0, 0, b"\x01", 0x10)), "b.wmf")
    embeds.draw()
    assert [b.text for b in embeds.blocks] == ["Logo"]
    assert sorted(embeds.needs()) == [
        f"1 emf embedded bitmap not read ({DRAW_HINT})",
        f"1 wmf image not read ({DRAW_HINT})",
    ]


def test_pure_python_render_draws_when_libreoffice_is_missing(monkeypatch):
    drawn = card("OUTLINE TEXT", (300, 100))
    seen = []
    monkeypatch.setattr(render, "available", lambda: False)
    monkeypatch.setattr(metafile, "replay_available", lambda: True)
    monkeypatch.setattr(metafile, "replay", lambda data: seen.append(data) or drawn)
    embeds = Embeds()
    outlines = emf(emr(27, struct.pack("<ii", 0, 0)))
    embeds.picture(Src("a.pptx", slide=1, img=1), outlines, "a.emf")
    embeds.picture(Src("a.pptx", slide=1, img=2), emf(text_w(0, 0, "Native")), "b.emf")
    embeds.draw()
    # Only the picture without text records is drawn
    assert seen == [outlines]
    assert [(j.src.cite(), j.data) for j in embeds.jobs] == [("a.pptx#slide1#img1", drawn)]
    assert [b.text for b in embeds.blocks] == ["Native"]


def test_libreoffice_failures_fall_back_to_the_pure_python_render(monkeypatch):
    monkeypatch.setattr(render, "available", lambda: True)
    monkeypatch.setattr(render, "to_pdfs", lambda *a, **k: {})
    monkeypatch.setattr(metafile, "replay_available", lambda: True)
    monkeypatch.setattr(metafile, "replay", lambda data: None)
    embeds = Embeds()
    embeds.picture(Src("a.docx", img=1), emf(), "a.emf")
    embeds.draw()
    assert embeds.needs() == ["1 emf image not read (no renderer could draw it)"]


def test_replay_renders_with_metafile_render():
    pytest.importorskip("metafile_render")
    png = metafile.replay(emf(font(1, 40), select(1), text_w(10, 10, "Hello")))
    assert png is not None and png.startswith(b"\x89PNG")
    assert metafile.replay(b"junk") is None


def test_standalone_pictures_melt_by_suffix_and_magic(tmp_path, no_renderer):
    data = wmf(wmf_textout(0, 0, b"Floor plan"))
    (tmp_path / "plan.wmz").write_bytes(gzip.compress(data))
    (tmp_path / "plan").write_bytes(data)
    (tmp_path / "chart.emf").write_bytes(emf(text_w(5, 5, "Chart title")))
    for name, text in (
        ("plan.wmz", "Floor plan"),
        ("plan", "Floor plan"),
        ("chart.emf", "Chart title"),
    ):
        out = metafile.convert(tmp_path / name, Src(name))
        assert out.kind == "metafile"
        assert [(b.src.cite(), b.text) for b in out.blocks] == [(name, text)], name
        assert out.needs == []
    head = (tmp_path / "plan").read_bytes()
    assert metafile.sniff(tmp_path / "plan", head)
    assert metafile.sniff(tmp_path / "chart.emf", (tmp_path / "chart.emf").read_bytes())
    assert not metafile.sniff(tmp_path / "x", b"PK\x03\x04")


def test_unreadable_standalone_picture_is_a_need(tmp_path, no_renderer):
    (tmp_path / "a.emz").write_bytes(b"not gzip")
    assert metafile.convert(tmp_path / "a.emz", Src("a.emz")).needs == [
        "1 unreadable image not read"
    ]


def test_shallow_draws_no_picture_and_still_lists_it(monkeypatch):

    monkeypatch.setattr("meltify.converters.run.current", lambda: RunContext(shallow=True))
    monkeypatch.setattr(render, "available", lambda: True)
    monkeypatch.setattr(render, "to_pdfs", lambda *a, **k: pytest.fail("started a renderer"))
    monkeypatch.setattr(metafile, "replay_available", lambda: True)
    monkeypatch.setattr(metafile, "replay", lambda data: pytest.fail("replayed a picture"))
    embeds = Embeds()
    embeds.picture(Src("a.docx", img=1), emf(emr(27, struct.pack("<ii", 0, 0))), "a.emf")
    embeds.picture(Src("a.docx", img=2), emf(text_w(0, 0, "Native")), "b.emf")
    embeds.draw()
    # Text records still read, while the picture without them waits for a deep read
    assert [b.text for b in embeds.blocks] == ["Native"]
    assert embeds.jobs == []
    assert embeds.needs() == ["1 emf image not read (not drawn under --shallow)"]
