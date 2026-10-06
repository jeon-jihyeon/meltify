import struct
import zipfile

import pytest

from meltify import imaging
from meltify.converters import pdf, raster
from meltify.engines import ocr as engines
from meltify.engines.ocr import LOCAL, TextBox
from meltify.evidence import Src
from tests.fixtures.make_docs import card


def _scan_pdf(path, header=None, layer=None, under=False):
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    if under:
        page.insert_text((72, 300), layer, fontsize=12)
    page.insert_image(page.rect, stream=card("SCANNED BODY 4,321", (850, 1100)))
    if header:
        page.insert_text((72, 30), header, fontsize=12)
    if layer and not under:
        page.insert_text((72, 300), layer, fontsize=12, render_mode=3)
    doc.save(path)
    return path


def test_scan_under_a_visible_header_is_read(tmp_path):
    path = _scan_pdf(tmp_path / "fax.pdf", header="Received by fax on 2026-10-01 from ACME")
    out = pdf.convert(path, Src("fax.pdf"))
    assert [b.src.cite() for b in out.blocks] == ["fax.pdf#p1"]
    assert [j.src.cite() for j in out.jobs] == ["fax.pdf#p1#img1"]
    # Kept at its aspect ratio, the scan fills the page width
    assert out.jobs[0].rect == (0, 36, 595, 806)


@pytest.mark.parametrize("under", [False, True])
def test_scan_with_an_ocr_layer_is_not_read_twice(tmp_path, under):
    layer = "SCANNED BODY 4,321 as the scanner read it"
    path = _scan_pdf(
        tmp_path / "s.pdf", header="Received by fax on 2026-10-01", layer=layer, under=under
    )
    out = pdf.convert(path, Src("s.pdf"))
    assert out.jobs == []
    assert layer in out.blocks[0].text


def test_annotation_comments_are_cited_where_they_sit(tmp_path):
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Body text of the page with enough chars", fontsize=12)
    note = page.add_text_annot((300, 300), "Sticky note body")
    note.set_info(title="Alice")
    note.update()
    page.add_freetext_annot(pymupdf.Rect(72, 400, 300, 450), "Freetext visible words").update()
    mark = page.add_highlight_annot(pymupdf.Rect(72, 60, 200, 76))
    mark.set_info(content="Check this number")
    mark.update()
    page.add_file_annot((400, 400), b"pinned payload\n", "pinned.txt").update()
    doc.embfile_add("table.csv", b"a,b\n1,2\n", filename="table.csv")
    doc.save(tmp_path / "notes.pdf")

    out = pdf.convert(tmp_path / "notes.pdf", Src("notes.pdf"))
    shown = {b.src.cite(): b.text for b in out.blocks}
    assert "Freetext visible words" in shown["notes.pdf#p1"]
    assert shown["notes.pdf#p1@pt(300,300,316,316)"] == "[Text by Alice] Sticky note body"
    assert any(t == "[Highlight] Check this number" for t in shown.values())
    # FreeText already shows on the page, so it isn't repeated as a note
    assert sum("Freetext visible words" in t for t in shown.values()) == 1
    assert {(c.name, c.data) for c in out.children} == {
        ("table.csv", b"a,b\n1,2\n"),
        ("pinned.txt", b"pinned payload\n"),
    }


def _xps(path):
    import pymupdf

    page = (
        '<FixedPage xmlns="http://schemas.microsoft.com/xps/2005/06" Width="816" Height="1056"'
        ' xml:lang="en-US"><Glyphs Fill="#ff000000" FontUri="/font.ttf"'
        ' FontRenderingEmSize="16" OriginX="96" OriginY="96"'
        ' UnicodeString="Hello from an XPS page body"/></FixedPage>'
    )
    ns = "http://schemas.microsoft.com/xps/2005/06"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(
            "_rels/.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="r1" Target="/doc.fdseq" Type="http://schemas.microsoft.com/xps/'
            '2005/06/fixedrepresentation"/></Relationships>',
        )
        z.writestr(
            "doc.fdseq",
            f'<FixedDocumentSequence xmlns="{ns}"><DocumentReference Source="/d.fdoc"/>'
            "</FixedDocumentSequence>",
        )
        z.writestr(
            "d.fdoc",
            f'<FixedDocument xmlns="{ns}"><PageContent Source="/p1.fpage"/></FixedDocument>',
        )
        z.writestr("p1.fpage", page)
        z.writestr("font.ttf", pymupdf.Font("helv").buffer)
    return path


def _mobi(path, html):
    # PalmDB header, then record 0 with an uncompressed PalmDOC and MOBI header, then the text
    mobi = b"MOBI" + struct.pack(">III", 232, 2, 65001)
    rec0 = struct.pack(">HHIHHHH", 1, 0, len(html), 1, 4096, 0, 0) + mobi.ljust(232, b"\0")
    records = [rec0, html]
    head = b"book".ljust(32, b"\0") + bytes(28) + b"BOOKMOBI" + bytes(8) + struct.pack(">H", 2)
    offset = len(head) + 8 * len(records) + 2
    table = b""
    for i, r in enumerate(records):
        table += struct.pack(">II", offset, i)
        offset += len(r)
    path.write_bytes(head + table + b"\0\0" + b"".join(records))
    return path


def test_mupdf_formats_read_text_with_page_cites(tmp_path):
    fb2 = tmp_path / "book.fb2"
    fb2.write_text(
        '<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0"><body><section>'
        "<p>Chapter one opens on a quiet harbor town</p></section></body></FictionBook>"
    )
    mobi = _mobi(
        tmp_path / "book.mobi", b"<html><body><p>Hello mobi world chapter one</p></body></html>"
    )
    xps = _xps(tmp_path / "page.xps")
    oxps = xps.with_suffix(".oxps")
    oxps.write_bytes(xps.read_bytes())
    for path, words in [
        (fb2, "quiet harbor town"),
        (mobi, "Hello mobi world"),
        (xps, "XPS page body"),
        (oxps, "XPS page body"),
    ]:
        out = pdf.convert(path, Src(path.name))
        assert out.blocks[0].src.cite() == f"{path.name}#p1"
        assert words in out.blocks[0].text
        assert (out.jobs, out.needs, out.children) == ([], [], [])


def test_comic_pages_go_to_page_ocr(tmp_path):
    cbz = tmp_path / "strip.cbz"
    with zipfile.ZipFile(cbz, "w") as z:
        z.writestr("01.png", card("PANEL ONE", (400, 600)))
        z.writestr("02.png", card("PANEL TWO", (400, 600)))
    out = pdf.convert(cbz, Src("strip.cbz"))
    assert [(j.kind, j.src.cite()) for j in out.jobs] == [
        ("page", "strip.cbz#p1"),
        ("page", "strip.cbz#p2"),
    ]
    assert imaging.render_pdf_page(str(cbz), 2, 72).size[0] > 0


def test_epub_keeps_its_own_converter():
    from meltify.converters import SUFFIXES

    assert ".epub" not in pdf.MUPDF
    assert SUFFIXES[".epub"].kind == "epub"


def _frames(path, shades, fmt):
    from PIL import Image, ImageDraw

    pictures = []
    for n, shade in enumerate(shades):
        im = Image.new("L", (200, 100), 255)
        ImageDraw.Draw(im).rectangle((10, 10, 10 + shade, 90), fill=0)
        # A stray pixel apart, since the GIF writer merges frames that are exactly alike
        im.putpixel((199, n), 0)
        pictures.append(im)
    pictures[0].save(path, format=fmt, save_all=True, append_images=pictures[1:])
    return path


def test_tiff_pages_each_get_a_frame_cite(tmp_path):
    path = _frames(tmp_path / "fax.tif", [20, 20, 120], "TIFF")
    out = raster.convert(path, Src("fax.tif"))
    # Pages of a document are never merged, even when two look alike
    assert [j.src.cite() for j in out.jobs] == [
        "fax.tif#frame1",
        "fax.tif#frame2",
        "fax.tif#frame3",
    ]
    assert out.needs == []


def test_animation_skips_repeats_and_caps_frames(tmp_path, monkeypatch):
    path = _frames(tmp_path / "a.gif", [20, 20, 120, 160, 180], "GIF")
    out = raster.convert(path, Src("a.gif"))
    assert [j.src.frame for j in out.jobs] == [1, 3, 4, 5]

    monkeypatch.setattr(imaging, "MAX_FRAMES", 2)
    out = raster.convert(path, Src("a.gif"))
    assert [j.src.frame for j in out.jobs] == [1, 3]
    assert out.needs == ["2 frames beyond 2 not read"]


def test_animation_frames_that_change_only_their_text_are_all_kept(tmp_path):
    import io

    from PIL import Image

    slides = [Image.open(io.BytesIO(card(t, (400, 120)))).convert("P") for t in ("A 101", "A 202")]
    path = tmp_path / "slides.gif"
    slides[0].save(path, save_all=True, append_images=slides[1:], duration=200)
    assert imaging.frames(path) == ([1, 2], 0)


def test_single_frame_image_keeps_its_plain_cite(tmp_path):
    (tmp_path / "a.png").write_bytes(card("ONE", (200, 80)))
    out = raster.convert(tmp_path / "a.png", Src("a.png"))
    assert [j.src.cite() for j in out.jobs] == ["a.png"]


class Shade:
    """Reads the mean gray level, so each frame it's shown gives its own text"""

    name = "vision"
    kind = LOCAL

    def missing(self):
        return None

    def recognize(self, image, size):
        from PIL import Image, ImageStat

        with Image.open(image) as im:
            return [TextBox(f"gray {round(ImageStat.Stat(im.convert('L')).mean[0])}", None, 0.9)]


def test_each_frame_is_read_and_cached_on_its_own(tmp_path, monkeypatch):
    from meltify.recognize import Options, Recognizer

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(engines, "select", lambda spec, s: [Shade()])
    path = _frames(tmp_path / "fax.tif", [20, 120], "TIFF")
    jobs = raster.convert(path, Src("fax.tif")).jobs
    got = Recognizer(Options(sharpen=False), {}, print).run(jobs)
    first, second = (o.blocks[0].text.splitlines()[0] for o in got)
    assert first != second
    assert [o.blocks[0].src.cite() for o in got] == ["fax.tif#frame1", "fax.tif#frame2"]


def _psd(width, height, gray):
    # Header, empty color mode, resources and layers sections, then raw planar RGB
    head = b"8BPS" + struct.pack(">H6xHIIHH", 1, 3, height, width, 8, 3)
    return head + struct.pack(">IIIH", 0, 0, 0, 0) + bytes([gray]) * (width * height * 3)


@pytest.mark.parametrize(
    "suffix, save",
    [
        (".jp2", {"format": "JPEG2000"}),
        (".j2k", {"format": "JPEG2000", "no_jp2": True}),
        (".ico", {"format": "ICO"}),
        (".tga", {"format": "TGA"}),
        (".psd", None),
    ],
)
def test_more_pillow_formats_open_for_ocr(tmp_path, suffix, save):
    from PIL import Image

    from meltify.recognize import Options, Recognizer

    path = tmp_path / f"a{suffix}"
    if save is None:
        path.write_bytes(_psd(120, 60, 200))
    else:
        Image.new("RGB", (120, 60), (200, 200, 200)).save(path, **save)
    (job,) = raster.convert(path, Src(path.name)).jobs
    image = Recognizer(Options(), {}, print)._load(job)
    assert image.mode == "RGB" and image.width > 0


def test_read_cites_notes_melts_attachments_and_reads_the_faxed_body(tmp_path, monkeypatch, capsys):
    import json
    from pathlib import Path

    import pymupdf

    from meltify.cli import main

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(engines, "select", lambda spec, s: [Shade()])
    path = _scan_pdf(tmp_path / "fax.pdf", header="Received by fax on 2026-10-01 from ACME")
    doc = pymupdf.open(path)
    doc[0].add_text_annot((300, 20), "Call them back").update()
    doc.embfile_add("table.csv", b"a,b\n1,2\n", filename="table.csv")
    doc.saveIncr()
    doc.close()

    assert main(["read", str(path), "--json", "--limit", "0"]) == 0
    rows = {r["cite"]: r for r in json.loads(capsys.readouterr().out)["results"]}
    assert rows[str(path)]["needs"] == []
    md = Path(rows[str(path)]["out"]).read_text()
    assert f"## {path}#p1@pt(300,20,316,36)\n[Text] Call them back\n" in md
    assert f"## {path}#p1#img1\n" in md
    child = rows[f"{path}#att=table.csv"]
    assert child["kind"] == "text" and "1,2" in Path(child["out"]).read_text()


def test_one_oversized_or_broken_attachment_doesnt_cost_the_rest(tmp_path, monkeypatch):
    import pymupdf

    path = tmp_path / "a.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Cover letter with three attachments")
    doc.embfile_add("big.bin", b"x" * 2048, filename="big.bin")
    doc.embfile_add("bad.csv", b"a,b", filename="bad.csv")
    doc.embfile_add("ok.csv", b"a,b\n1,2", filename="ok.csv")
    doc.save(path)
    doc.close()

    get = pymupdf.Document.embfile_get

    def flaky(self, name):
        if name == "bad.csv":
            raise pymupdf.FileDataError("bad stream")
        return get(self, name)

    monkeypatch.setattr(pdf, "MAX_MEMBER_BYTES", 1024)
    monkeypatch.setattr(pymupdf.Document, "embfile_get", flaky)
    out = pdf.convert(path, Src("a.pdf"))
    assert [c.name for c in out.children] == ["ok.csv"]
    assert sorted(out.needs) == [
        "attachment bad.csv not read (FileDataError: bad stream)",
        "attachment big.bin not read (over 1024 bytes)",
    ]
