import io
import json
from pathlib import Path

import pytest
from PIL import Image

from meltify.cli import main
from meltify.converters import metafile, pick
from meltify.converters.rtf import convert
from meltify.evidence import Src
from tests.fixtures.make_docs import card

KOREAN = (
    b"{\\rtf1\\ansi\\ansicpg949\\deff0{\\fonttbl{\\f0 Batang;}}\n"
    b"\\f0 Hello RTF\\par\n"
    b"\\'c7\\'d1\\'b1\\'db cp949 bytes\\par\n"
    b"\\uc1\\u54620?\\u44544? unicode escapes\\par\n"
    b"{\\*\\generator hidden}last line\\par\n}"
)


def test_codepage_bytes_and_unicode_escapes_decode(tmp_path):
    path = tmp_path / "a.rtf"
    path.write_bytes(KOREAN)
    (block,) = convert(path, Src("a.rtf")).blocks
    assert block.src.cite() == "a.rtf:1"
    assert block.text.splitlines() == [
        "1| Hello RTF",
        "2| 한글 cp949 bytes",
        "3| 한글 unicode escapes",
        "4| last line",
    ]


def test_rtf_without_suffix_is_sniffed(tmp_path):
    path = tmp_path / "note"
    path.write_bytes(KOREAN)
    assert pick(path)[0] == "rtf"


def test_read_cites_rtf_end_to_end(tmp_path, monkeypatch, capsys):
    (tmp_path / "a.rtf").write_bytes(KOREAN)
    monkeypatch.chdir(tmp_path)
    main(["read", "a.rtf", "--json", "--limit", "0"])
    (row,) = json.loads(capsys.readouterr().out)["results"]
    assert row["kind"] == "rtf"
    assert "## a.rtf:1\n1| Hello RTF\n2| 한글 cp949 bytes" in Path(row["out"]).read_text()


def _pictures_rtf() -> bytes:
    png = card("INVOICE 2026", (300, 100))
    jpeg = io.BytesIO()
    Image.open(io.BytesIO(png)).save(jpeg, "JPEG")
    hexed = png.hex()
    wrapped = "\n".join(hexed[i : i + 64] for i in range(0, len(hexed), 64))
    return (
        b"{\\rtf1\\ansi{\\fonttbl{\\f0 Arial;}}\n"
        b"First paragraph\\par\n"
        # Word writes the PNG once for new readers and again as WMF for old ones
        b"{\\*\\shppict{\\pict{\\*\\picprop{\\sp{\\sn x}{\\sv 1}}}\\pngblip\\picw300 "
        + wrapped.encode()
        + b"}}{\\nonshppict{\\pict\\wmetafile8 0100090000}}\\par\n"
        b"Third {\\pict\\jpegblip\\bin"
        + str(len(jpeg.getvalue())).encode()
        + b" "
        + jpeg.getvalue()
        + b"}\\par\n"
        b"{\\pict\\emfblip 0100000088}{\\pict\\macpict 0011}{\\pict\\pngblip 0g}\\par\n"
        b"Last line\\par\n}"
    )


def test_pictures_cite_their_paragraph_and_skip_the_old_reader_copy(tmp_path, monkeypatch):
    from meltify.converters import embeds, render

    monkeypatch.setattr(render, "available", lambda: False)
    monkeypatch.setattr(metafile, "replay_available", lambda: False)
    path = tmp_path / "a.rtf"
    path.write_bytes(_pictures_rtf())
    out = convert(path, Src("a.rtf"))
    assert [j.src.cite() for j in out.jobs] == ["a.rtf#para2#img1", "a.rtf#para3#img2"]
    assert out.jobs[0].data == card("INVOICE 2026", (300, 100))
    assert out.jobs[1].data.startswith(b"\xff\xd8\xff")
    assert sorted(out.needs) == [
        f"1 emf image not read ({embeds.DRAW_HINT})",
        "1 pict image not read",
        "1 unreadable image not read",
    ]
    # The text around the pictures is still all there
    assert "Last line" in "\n".join(b.text for b in out.blocks)


def test_metafiles_are_drawn_when_libreoffice_is_there(tmp_path, monkeypatch):
    from meltify.converters import render

    drawn = card("EMF CHART", (300, 100))

    def to_pdfs(paths, out_dir, timeout=0):
        return {p: p.with_suffix(".pdf") for p in paths}

    monkeypatch.setattr(render, "available", lambda: True)
    monkeypatch.setattr(render, "to_pdfs", to_pdfs)
    monkeypatch.setattr(render, "png", lambda pdf, *a, **k: drawn)
    path = tmp_path / "a.rtf"
    path.write_bytes(b"{\\rtf1 Intro\\par{\\pict\\emfblip 0100000088}\\par}")
    out = convert(path, Src("a.rtf"))
    assert [(j.src.cite(), j.data) for j in out.jobs] == [("a.rtf#para2#img1", drawn)]
    assert out.needs == []


def test_cut_off_picture_is_still_reported(tmp_path):
    path = tmp_path / "a.rtf"
    path.write_bytes(b"{\\rtf1 Intro\\par{\\pict\\pngblip 89504e")
    out = convert(path, Src("a.rtf"))
    assert out.jobs == []
    assert out.needs == ["1 unreadable image not read"]
    assert [b.text for b in out.blocks] == ["1| Intro"]


def test_metafile_text_is_read_without_a_renderer(tmp_path, monkeypatch):
    from meltify.converters import render
    from tests.fixtures.make_binary import wmf, wmf_textout

    monkeypatch.setattr(render, "available", lambda: pytest.fail("text records need no render"))
    picture = wmf(wmf_textout(0, 0, b"Q3 revenue 4,210")).hex()
    path = tmp_path / "a.rtf"
    path.write_bytes(b"{\\rtf1 Intro\\par{\\pict\\wmetafile8 " + picture.encode() + b"}\\par}")
    out = convert(path, Src("a.rtf"))
    assert [(b.src.cite(), b.text) for b in out.blocks[1:]] == [
        ("a.rtf#para2#img1", "Q3 revenue 4,210")
    ]
    assert (out.jobs, out.needs) == ([], [])


@pytest.mark.parametrize("data", [b"", b"{\\rtf1\\ansi{\\fonttbl{\\f0 Arial;}}", b"{\\rtf1 }"])
def test_rtf_without_text_is_a_need(tmp_path, data):
    path = tmp_path / "a.rtf"
    path.write_bytes(data)
    out = convert(path, Src("a.rtf"))
    assert (out.blocks, out.jobs) == ([], [])
    assert out.needs == ["rtf has no text"]
