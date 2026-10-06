import io
import os
import re
import zipfile
from pathlib import Path

import pytest

from meltify.converters import (
    limits,
    metafile,
    office,
    ooxml,
    ooxml_charts,
    pick,
    render,
    sheet,
    xmlsafe,
)
from meltify.converters.embeds import DRAW_HINT, Embeds
from meltify.converters.run import RunContext
from meltify.evidence import Src
from tests.fixtures.make_docs import card

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
DGM = "http://schemas.openxmlformats.org/drawingml/2006/diagram"
ASVG = "http://schemas.microsoft.com/office/drawing/2016/SVG/main"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
DOCX_MAIN = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
DOTX_MAIN = "application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml"
PPTX_MAIN = "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"

CHART = (
    f'<c:chartSpace xmlns:c="{C}" xmlns:a="{A}"><c:chart>'
    "<c:title><c:tx><c:rich><a:p><a:r><a:t>Revenue</a:t></a:r></a:p></c:rich></c:tx></c:title>"
    "<c:plotArea><c:barChart><c:ser>"
    "<c:tx><c:strRef><c:f>Sheet1!$B$1</c:f><c:strCache><c:ptCount val='1'/>"
    "<c:pt idx='0'><c:v>2026</c:v></c:pt></c:strCache></c:strRef></c:tx>"
    "<c:cat><c:strRef><c:strCache><c:ptCount val='2'/><c:pt idx='0'><c:v>Q1</c:v></c:pt>"
    "<c:pt idx='1'><c:v>Q|2</c:v></c:pt></c:strCache></c:strRef></c:cat>"
    "<c:val><c:numRef><c:numCache><c:ptCount val='2'/><c:pt idx='0'><c:v>10</c:v></c:pt>"
    "<c:pt idx='1'><c:v>20.5</c:v></c:pt></c:numCache></c:numRef></c:val>"
    "</c:ser></c:barChart>"
    "<c:catAx><c:title><c:tx><c:rich><a:p><a:r><a:t>Quarter</a:t></a:r></a:p></c:rich></c:tx>"
    "</c:title></c:catAx></c:plotArea></c:chart></c:chartSpace>"
)
EMPTY_CHART = (
    f'<c:chartSpace xmlns:c="{C}"><c:chart><c:plotArea><c:lineChart><c:ser>'
    "<c:val><c:numRef><c:f>Sheet1!$B$2:$B$3</c:f></c:numRef></c:val>"
    "</c:ser></c:lineChart></c:plotArea></c:chart></c:chartSpace>"
)


def _t(text: str) -> str:
    return f"<a:p><a:r><a:t>{text}</a:t></a:r></a:p>"


SMARTART = (
    f'<dgm:dataModel xmlns:dgm="{DGM}" xmlns:a="{A}"><dgm:ptLst>'
    '<dgm:pt modelId="0" type="doc"/>'
    f'<dgm:pt modelId="1"><dgm:t>{_t("CEO")}</dgm:t></dgm:pt>'
    f'<dgm:pt modelId="2"><dgm:t>{_t("CTO")}</dgm:t></dgm:pt>'
    f'<dgm:pt modelId="3"><dgm:t>{_t("CFO")}</dgm:t></dgm:pt>'
    f'<dgm:pt modelId="4" type="pres"><dgm:t>{_t("layout only")}</dgm:t></dgm:pt>'
    f'<dgm:pt modelId="5"><dgm:t>{_t("Advisor")}</dgm:t></dgm:pt>'
    "</dgm:ptLst><dgm:cxnLst>"
    '<dgm:cxn modelId="10" srcId="0" destId="1" srcOrd="0"/>'
    '<dgm:cxn modelId="11" srcId="1" destId="3" srcOrd="1"/>'
    '<dgm:cxn modelId="12" srcId="1" destId="2" srcOrd="0"/>'
    '<dgm:cxn modelId="13" type="presOf" srcId="1" destId="4"/>'
    "</dgm:cxnLst></dgm:dataModel>"
)
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><text x="1" y="9">Org label</text></svg>'


def _graphic(inner: str) -> str:
    return (
        f'<w:p><w:r><w:drawing><a:graphic xmlns:a="{A}"><a:graphicData>{inner}'
        "</a:graphicData></a:graphic></w:drawing></w:r></w:p>"
    )


def _rel(rid: str, target: str) -> str:
    return f'<Relationship Id="{rid}" Type="{R}/x" Target="{target}"/>'


def regions_docx(path: Path, main: str = DOCX_MAIN, chart: str = CHART) -> Path:
    """Paragraph 2 holds a chart, 3 a SmartArt, 4 an SVG with its PNG fallback, 5 an EMF"""
    body = [
        "<w:p><w:r><w:t>Intro paragraph</w:t></w:r></w:p>",
        _graphic(f'<c:chart xmlns:c="{C}" r:id="rId1"/>'),
        _graphic(f'<dgm:relIds xmlns:dgm="{DGM}" r:dm="rId2" r:lo="rId9" r:qs="" r:cs=""/>'),
        _graphic(
            '<a:blip r:embed="rId3"><a:extLst><a:ext uri="{96DAC541-7B7A-43D3-8B79-37D633B846F1}">'
            f'<asvg:svgBlip xmlns:asvg="{ASVG}" r:embed="rId4"/></a:ext></a:extLst></a:blip>'
        ),
        _graphic('<a:blip r:embed="rId5"/>'),
    ]
    document = (
        f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{W}" xmlns:r="{R}">'
        f"<w:body>{''.join(body)}</w:body></w:document>"
    )
    rels = "".join(
        [
            _rel("rId1", "charts/chart1.xml"),
            _rel("rId2", "diagrams/data1.xml"),
            _rel("rId3", "media/image1.png"),
            _rel("rId4", "media/image2.svg"),
            _rel("rId5", "media/image3.emf"),
        ]
    )
    types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/'
        'vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f'<Override PartName="/word/document.xml" ContentType="{main}"/></Types>'
    )
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", types)
        z.writestr(
            "_rels/.rels",
            f'<Relationships xmlns="{PKG}">'
            f'<Relationship Id="rId1" Type="{R}/officeDocument" Target="word/document.xml"/>'
            "</Relationships>",
        )
        z.writestr("word/document.xml", document)
        z.writestr(
            "word/_rels/document.xml.rels", f'<Relationships xmlns="{PKG}">{rels}</Relationships>'
        )
        z.writestr("word/charts/chart1.xml", chart)
        z.writestr("word/diagrams/data1.xml", SMARTART)
        z.writestr("word/media/image1.png", card("SVG FALLBACK", (300, 100)))
        z.writestr("word/media/image2.svg", SVG)
        z.writestr("word/media/image3.emf", b"\x01\x00\x00\x00 not really emf")
    return path


def chart_pptx(path: Path, slides: int = 1) -> Path:
    """The chart sits on the last slide, one inch in and two inches down on a 10 by 7.5 deck"""
    pytest.importorskip("pptx")
    from pptx import Presentation
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.util import Inches

    deck = Presentation()
    for n in range(1, slides + 1):
        slide = deck.slides.add_slide(deck.slide_layouts[5])
        slide.shapes.title.text = f"Slide {n}"
    data = CategoryChartData()
    data.categories = ["Q1", "Q2"]
    data.add_series("Seoul", (10, 20))
    frame = slide.shapes.add_chart(
        XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(2), Inches(5), Inches(3), data
    )
    frame.chart.has_title = True
    frame.chart.chart_title.text_frame.text = "Sales"
    deck.save(path)
    return path


def _rewrite(path: Path, member: str, change) -> Path:
    with zipfile.ZipFile(path) as z:
        items = [(i, z.read(i)) for i in z.infolist()]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for info, data in items:
            z.writestr(info, change(data) if info.filename == member else data)
    return path


def _drop_caches(data: bytes) -> bytes:
    return re.sub(rb"<c:(str|num)Cache>.*?</c:\1Cache>", b"", data, flags=re.S)


def _drop_data(data: bytes) -> bytes:
    """No cached values and no link to the embedded workbook either"""
    data = re.sub(rb"<c:externalData\b[^>]*?(/>|>.*?</c:externalData>)", b"", data, flags=re.S)
    return _drop_caches(data)


def _fake_pdfs(fail: bool = False):
    def to_pdfs(paths, out_dir, timeout=render.TIMEOUT):
        import pymupdf

        if fail:
            raise RuntimeError("soffice exited 1")
        out_dir.mkdir(parents=True, exist_ok=True)
        out = {}
        for p in paths:
            doc = pymupdf.open()
            page = doc.new_page(width=300, height=100)
            page.insert_text((20, 50), f"VECTOR {p.stem}", fontsize=24)
            doc.save(out_dir / f"{p.stem}.pdf")
            out[p] = out_dir / f"{p.stem}.pdf"
        return out

    return to_pdfs


@pytest.fixture(autouse=True)
def no_replay(monkeypatch):
    # metafile-render may or may not be installed, so tests opt in to it
    monkeypatch.setattr(metafile, "replay_available", lambda: False)


def test_docx_charts_diagrams_and_svg_are_read_natively(tmp_path, monkeypatch):
    monkeypatch.setattr(render, "available", lambda: False)
    got = office.docx_images(regions_docx(tmp_path / "memo.docx"), Src("memo.docx"))
    got.draw()
    blocks = {b.src.cite(): b.text for b in got.blocks}
    assert blocks["memo.docx#para2#img1"] == (
        "Chart: Revenue\nAxes: Quarter\n| category | 2026 |\n|---|---|\n"
        "| Q1 | 10 |\n| Q\\|2 | 20.5 |"
    )
    assert blocks["memo.docx#para3#img2"] == "Diagram\n- CEO\n  - CTO\n  - CFO\n- Advisor"
    # The SVG goes on to its own converter, and its PNG fallback isn't read twice
    assert [(c.name, c.data) for c in got.children] == [("word/media/image2.svg", SVG)]
    assert got.jobs == []
    assert got.needs() == [f"1 emf image not read ({DRAW_HINT})"]


def test_emf_is_rendered_through_libreoffice_and_queued_for_ocr(tmp_path, monkeypatch):
    monkeypatch.setattr(render, "available", lambda: True)
    monkeypatch.setattr(render, "to_pdfs", _fake_pdfs())
    got = office.docx_images(regions_docx(tmp_path / "memo.docx"), Src("memo.docx"))
    got.draw()
    assert [j.src.cite() for j in got.jobs] == ["memo.docx#para5#img4"]
    assert got.jobs[0].data.startswith(b"\x89PNG")
    assert got.needs() == []


def test_a_failed_render_stays_listed(tmp_path, monkeypatch):
    monkeypatch.setattr(render, "available", lambda: True)
    monkeypatch.setattr(render, "to_pdfs", _fake_pdfs(fail=True))
    got = office.docx_images(regions_docx(tmp_path / "memo.docx"), Src("memo.docx"))
    got.draw()
    assert got.jobs == []
    assert got.needs() == ["1 emf image not read (no renderer could draw it)"]


def test_gzipped_emf_is_unpacked_before_rendering(monkeypatch):
    import gzip

    monkeypatch.setattr(render, "available", lambda: True)
    seen = []
    fake = _fake_pdfs()
    monkeypatch.setattr(
        render,
        "to_pdfs",
        lambda paths, *a: seen.extend(p.read_bytes() for p in paths) or fake(paths, *a),
    )
    embeds = Embeds()
    embeds.picture(Src("a.docx", img=1), gzip.compress(b"EMF BYTES"), "media/a.emz")
    embeds.picture(Src("a.docx", img=2), b"not gzip", "media/b.wmz")
    embeds.draw()
    assert seen == [b"EMF BYTES"]
    assert len(embeds.jobs) == 1
    assert embeds.needs() == ["1 unreadable image not read"]


def test_docx_chart_without_cache_is_listed(tmp_path, monkeypatch):
    monkeypatch.setattr(render, "available", lambda: False)
    got = office.docx_images(
        regions_docx(tmp_path / "memo.docx", chart=EMPTY_CHART), Src("memo.docx")
    )
    got.draw()
    assert "memo.docx#para2#img1" not in {b.src.cite() for b in got.blocks}
    assert "1 chart not read (no cached values)" in got.needs()


def test_template_and_macro_names_melt_like_their_family(tmp_path, monkeypatch):
    pytest.importorskip("markitdown")
    monkeypatch.setattr(render, "available", lambda: False)
    for name in ("memo.dotx", "memo.docm", "memo.dotm"):
        path = regions_docx(tmp_path / name, main=DOTX_MAIN)
        out = office.convert(path, Src(name))
        assert "Intro paragraph" in out.blocks[0].text, name
        assert f"{name}#para2#img1" in {b.src.cite() for b in out.blocks}


def test_pptx_chart_is_one_cited_block_and_not_a_need(tmp_path):
    pytest.importorskip("markitdown")
    path = chart_pptx(tmp_path / "deck.pptx")
    out = office.convert(path, Src("deck.pptx"))
    assert "Slide 1" in out.blocks[0].text
    # markitdown's own chart table would repeat the cited block below
    assert "### Chart" not in out.blocks[0].text
    assert [(b.src.cite(), b.text) for b in out.blocks[1:]] == [
        (
            "deck.pptx#slide1#img1",
            "Chart: Sales\n| category | Seoul |\n|---|---|\n| Q1 | 10 |\n| Q2 | 20 |",
        )
    ]
    assert out.needs == []


@pytest.mark.parametrize(
    "name,main",
    [
        (
            "deck.potx",
            "application/vnd.openxmlformats-officedocument.presentationml.template.main+xml",
        ),
        (
            "deck.ppsx",
            "application/vnd.openxmlformats-officedocument.presentationml.slideshow.main+xml",
        ),
        ("deck.ppsm", "application/vnd.ms-powerpoint.slideshow.macroEnabled.main+xml"),
        ("deck.potm", "application/vnd.ms-powerpoint.template.macroEnabled.main+xml"),
        ("deck.pptm", "application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml"),
        ("deck.show", PPTX_MAIN),
    ],
)
def test_slideshows_templates_and_hancom_show_open_as_decks(tmp_path, name, main):
    pytest.importorskip("markitdown")
    path = chart_pptx(tmp_path / name)
    _rewrite(path, "[Content_Types].xml", lambda d: d.replace(PPTX_MAIN.encode(), main.encode()))
    out = office.convert(path, Src(name))
    assert "Slide 1" in out.blocks[0].text
    assert out.blocks[1].src.cite() == f"{name}#slide1#img1"
    # The patch lives in memory only
    assert main.encode() in zipfile.ZipFile(path).read("[Content_Types].xml")


def test_uncached_chart_reads_its_embedded_workbook(tmp_path, monkeypatch):
    path = chart_pptx(tmp_path / "deck.pptx")
    _rewrite(path, "ppt/charts/chart1.xml", _drop_caches)
    monkeypatch.setattr(render, "to_pdf", lambda *a, **k: pytest.fail("read natively"))
    got = office.pptx_images(path, Src("deck.pptx"))
    got.draw()
    assert [(b.src.cite(), b.text) for b in got.blocks] == [
        (
            "deck.pptx#slide1#img1",
            "Chart: Sales\n| category | Seoul |\n|---|---|\n| Q1 | 10 |\n| Q2 | 20 |",
        )
    ]
    assert got.needs() == []


def test_uncached_pptx_chart_is_cropped_from_a_render(tmp_path, monkeypatch):
    path = chart_pptx(tmp_path / "deck.pptx", slides=2)
    _rewrite(path, "ppt/charts/chart1.xml", _drop_data)
    monkeypatch.setattr(render, "available", lambda: False)
    assert office.pptx_images(path, Src("deck.pptx")).needs() == [
        "1 chart not read (no cached values)"
    ]

    pages = []

    def to_pdf(p, timeout=render.TIMEOUT, out_dir=None):
        import pymupdf

        doc = pymupdf.open()
        for _ in range(2):
            pages.append(doc.new_page(width=720, height=540))
        doc.save(out_dir / "deck.pdf")
        return out_dir / "deck.pdf"

    monkeypatch.setattr(render, "available", lambda: True)
    monkeypatch.setattr(render, "to_pdf", to_pdf)
    got = office.pptx_images(path, Src("deck.pptx"))
    got.draw()
    assert got.needs() == []
    assert [j.src.cite() for j in got.jobs] == ["deck.pptx#slide2#img1"]
    from PIL import Image

    # 5 by 3 inches at the render's 200 dpi
    assert Image.open(io.BytesIO(got.jobs[0].data)).size == (1000, 600)


def test_hidden_slide_charts_have_no_page_to_crop(tmp_path, monkeypatch):
    path = chart_pptx(tmp_path / "deck.pptx")
    _rewrite(path, "ppt/charts/chart1.xml", _drop_data)
    _rewrite(path, "ppt/slides/slide1.xml", lambda d: d.replace(b"<p:sld ", b'<p:sld show="0" ', 1))
    monkeypatch.setattr(render, "available", lambda: True)
    monkeypatch.setattr(render, "to_pdf", lambda *a, **k: pytest.fail("nothing to render"))
    assert office.pptx_images(path, Src("deck.pptx")).needs() == [
        "1 chart not read (no cached values)"
    ]


def chart_xlsx(path: Path, template: bool = False) -> Path:
    """openpyxl writes the chart with formulas only, anchored on Sales!D2"""
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, Reference

    wb = Workbook()
    ws = wb.active
    ws.title = "Sales 2026"
    for row in [("region", "amount"), ("Seoul", 100), ("Busan", 70)]:
        ws.append(row)
    chart = BarChart()
    chart.title = "By region"
    chart.add_data(Reference(ws, min_col=2, min_row=1, max_row=3), titles_from_data=True)
    chart.set_categories(Reference(ws, min_col=1, min_row=2, max_row=3))
    ws.add_chart(chart, "D2")
    wb.template = template
    wb.save(path)
    return path


def test_xlsx_chart_without_cache_reads_the_cells_it_names(tmp_path):
    out = sheet.convert(chart_xlsx(tmp_path / "book.xlsx"), Src("book.xlsx"))
    charts = [b for b in out.blocks if b.text.startswith("Chart")]
    assert [(b.src.cite(), b.text) for b in charts] == [
        (
            "book.xlsx#Sales 2026!D2#img1",
            "Chart: By region\n| category | amount |\n|---|---|\n| Seoul | 100 |\n| Busan | 70 |",
        )
    ]
    assert out.needs == []


@pytest.mark.parametrize("name", ["book.xltx", "book.xltm", "book.xlsm", "book.cell"])
def test_template_macro_and_hancom_cell_workbooks_open(tmp_path, name):
    path = chart_xlsx(tmp_path / "src.xlsx", template=name.startswith("book.xlt"))
    path = path.rename(tmp_path / name)
    out = sheet.convert(path, Src(name))
    assert out.blocks[0].src.cite() == f"{name}#Sales 2026"
    assert "| 2 | Seoul | 100 |" in out.blocks[0].text
    assert any(b.text.startswith("Chart: By region") for b in out.blocks)


def test_old_hancom_binaries_say_how_to_resave(tmp_path):
    (tmp_path / "a.show").write_bytes(b"\xd0\xcf\x11\xe0 old hancom")
    (tmp_path / "a.cell").write_bytes(b"\xd0\xcf\x11\xe0 old hancom")
    assert office.convert(tmp_path / "a.show", Src("a.show")).needs == [
        "Hancom .show before 2014 not read (save it as .pptx)"
    ]
    assert sheet.convert(tmp_path / "a.cell", Src("a.cell")).needs == [
        "Hancom .cell before 2014 not read (save it as .xlsx)"
    ]


def test_office_hands_a_misnamed_workbook_to_sheet(tmp_path):
    path = chart_xlsx(tmp_path / "src.xlsx").rename(tmp_path / "book.show")
    out = office.convert(path, Src("book.show"))
    assert out.kind == "sheet"
    assert out.blocks[0].src.cite() == "book.show#Sales 2026"


def test_chart_text_handles_literals_scatter_and_multi_level_categories():
    import xml.etree.ElementTree as ET

    chart = (
        f'<c:chartSpace xmlns:c="{C}"><c:chart><c:plotArea>'
        "<c:scatterChart><c:ser><c:tx><c:v>fit</c:v></c:tx>"
        "<c:xVal><c:numLit><c:pt idx='0'><c:v>1</c:v></c:pt><c:pt idx='2'><c:v>3</c:v></c:pt>"
        "</c:numLit></c:xVal>"
        "<c:yVal><c:numLit><c:ptCount val='3'/><c:pt idx='0'><c:v>2</c:v></c:pt>"
        "<c:pt idx='2'><c:v>6</c:v></c:pt></c:numLit></c:yVal></c:ser></c:scatterChart>"
        "<c:barChart><c:ser><c:cat><c:multiLvlStrRef><c:multiLvlStrCache><c:ptCount val='1'/>"
        "<c:lvl><c:pt idx='0'><c:v>Jan</c:v></c:pt></c:lvl>"
        "<c:lvl><c:pt idx='0'><c:v>2026</c:v></c:pt></c:lvl>"
        "</c:multiLvlStrCache></c:multiLvlStrRef></c:cat>"
        "<c:val><c:numRef><c:numCache><c:pt idx='0'><c:v>5</c:v></c:pt></c:numCache></c:numRef>"
        "</c:val></c:ser></c:barChart></c:plotArea></c:chart></c:chartSpace>"
    )
    assert ooxml_charts.chart_text(ET.fromstring(chart)) == (
        "Chart\n| category | fit | series 2 |\n|---|---|---|\n"
        "| 1 | 2 | 5 |\n| 2 |  |  |\n| 3 | 6 |  |"
    )


def test_huge_point_index_reads_sparse_instead_of_padding():
    import xml.etree.ElementTree as ET

    chart = (
        f'<c:chartSpace xmlns:c="{C}"><c:chart><c:plotArea><c:lineChart><c:ser><c:val><c:numLit>'
        "<c:pt idx='0'><c:v>1</c:v></c:pt><c:pt idx='999999999'><c:v>2</c:v></c:pt>"
        "</c:numLit></c:val></c:ser></c:lineChart></c:plotArea></c:chart></c:chartSpace>"
    )
    assert ooxml_charts.chart_text(ET.fromstring(chart)).endswith("| 1 | 1 |\n| 2 | 2 |")


def test_package_reads_family_from_content_types_not_names(tmp_path):
    docx = regions_docx(tmp_path / "noext")
    plain = tmp_path / "plain.zip"
    with zipfile.ZipFile(plain, "w") as z:
        z.writestr("a.txt", "hi")
    xlsx = chart_xlsx(tmp_path / "book.bin")
    assert pick(docx)[0] == "office" and pick(xlsx)[0] == "sheet"
    assert ooxml.family(plain) is None and pick(plain.rename(tmp_path / "plain"))[0] == "archive"
    assert ooxml.family(tmp_path / "missing") is None


def formula_xlsx(path: Path, values: bool = False) -> Path:
    """The chart plots formula cells, which openpyxl saves without results"""
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, Reference

    wb = Workbook()
    ws = wb.active
    ws.title = "Calc"
    for row in [("region", "double"), ("Seoul", 200 if values else "=50*4")]:
        ws.append(row)
    chart = BarChart()
    chart.add_data(Reference(ws, min_col=2, min_row=1, max_row=2), titles_from_data=True)
    chart.set_categories(Reference(ws, min_col=1, min_row=2, max_row=2))
    ws.add_chart(chart, "D2")
    wb.save(path)
    return path


def test_xlsx_chart_on_uncalculated_formulas_asks_libreoffice(tmp_path, monkeypatch):
    path = formula_xlsx(tmp_path / "book.xlsx")
    monkeypatch.setattr(render, "soffice", lambda: None)
    assert sheet.convert(path, Src("book.xlsx")).needs == ["1 chart not read (no cached values)"]

    calls = []

    def recalc(source, target, out_dir, timeout=render.TIMEOUT):
        calls.append((source, target))
        return formula_xlsx(out_dir / f"{source.stem}.xlsx", values=True)

    monkeypatch.setattr(render, "soffice", lambda: "/opt/soffice")
    monkeypatch.setattr(render, "convert_to", recalc)
    out = sheet.convert(path, Src("book.xlsx"))
    assert calls == [(path, "xlsx")]
    charts = [b for b in out.blocks if b.text.startswith("Chart")]
    assert [(b.src.cite(), b.text.splitlines()[-1]) for b in charts] == [
        ("book.xlsx#Calc!D2#img1", "| Seoul | 200 |")
    ]
    assert out.needs == []


def test_failed_recalculation_keeps_the_chart_listed(tmp_path, monkeypatch):
    def fail(*args):
        raise RuntimeError("soffice exited 1")

    monkeypatch.setattr(render, "soffice", lambda: "/opt/soffice")
    monkeypatch.setattr(render, "convert_to", fail)
    out = sheet.convert(formula_xlsx(tmp_path / "book.xlsx"), Src("book.xlsx"))
    assert out.needs == ["1 chart not read (no cached values)"]


def test_docx_chart_without_cache_reads_its_embedded_workbook(tmp_path, monkeypatch):
    monkeypatch.setattr(render, "available", lambda: False)
    chart = (
        f'<c:chartSpace xmlns:c="{C}" xmlns:r="{R}"><c:chart><c:plotArea><c:barChart><c:ser>'
        "<c:cat><c:strRef><c:f>Calc!$A$2</c:f></c:strRef></c:cat>"
        "<c:val><c:numRef><c:f>Calc!$B$2</c:f></c:numRef></c:val>"
        '</c:ser></c:barChart></c:plotArea></c:chart><c:externalData r:id="rId1"/>'
        "</c:chartSpace>"
    )
    path = regions_docx(tmp_path / "memo.docx", chart=chart)
    book = formula_xlsx(tmp_path / "embedded.xlsx", values=True)
    with zipfile.ZipFile(path, "a") as z:
        z.writestr(
            "word/charts/_rels/chart1.xml.rels",
            f'<Relationships xmlns="{PKG}">{_rel("rId1", "../embeddings/book.xlsx")}'
            "</Relationships>",
        )
        z.write(book, "word/embeddings/book.xlsx")
    got = office.docx_images(path, Src("memo.docx"))
    got.draw()
    blocks = {b.src.cite(): b.text for b in got.blocks}
    assert blocks["memo.docx#para2#img1"].splitlines()[-1] == "| Seoul | 200 |"
    assert "1 chart not read (no cached values)" not in got.needs()


def test_distinct_svgs_sharing_a_name_are_all_kept():
    embeds = Embeds()
    embeds.picture(Src("page.html", img=1), b"<svg><text>one</text></svg>", "inline.svg")
    embeds.picture(Src("page.html", img=2), b"<svg><text>two</text></svg>", "inline.svg")
    # The same picture drawn again holds the same text, so it's read once
    embeds.picture(Src("page.html", img=3), b"<svg><text>one</text></svg>", "copy.svg")
    assert [(c.name, c.parent.img) for c in embeds.children] == [
        ("inline.svg", 1),
        ("inline.svg", 2),
    ]


def test_main_part_comes_from_the_package_relationship_and_a_default_type(tmp_path):
    from meltify.converters import pick

    path = regions_docx(tmp_path / "memo.docx")
    # No Override, so only the relationship and the xml Default name the main part
    _rewrite(
        path,
        "[Content_Types].xml",
        lambda d: re.sub(rb"<Override[^>]*/>", b"", d).replace(
            b'ContentType="application/xml"', f'ContentType="{DOCX_MAIN}"'.encode()
        ),
    )
    with zipfile.ZipFile(path) as z:
        assert ooxml.package(z) == ooxml.Package("docx", "word/document.xml", DOCX_MAIN)
    unnamed = path.rename(tmp_path / "memo")
    assert pick(unnamed)[0] == "office"


def test_charts_sit_with_their_slide_not_after_every_slide():
    from meltify.converters import Block, Converted

    out = Converted(
        "odf", [Block(Src("a.odp", slide=1), "one"), Block(Src("a.odp", slide=2), "two")]
    )
    embeds = Embeds()
    embeds.blocks.append(Block(Src("a.odp", slide=1, img=1), "Chart"))
    embeds.into(out)
    assert [b.text for b in out.blocks] == ["one", "Chart", "two"]


def _fake_pdf(pages: list[tuple[float, float]], seen: list | None = None):
    def to_pdf(p, timeout=render.TIMEOUT, out_dir=None):
        import pymupdf

        if seen is not None:
            seen.append(zipfile.ZipFile(p).read("word/document.xml"))
        doc = pymupdf.open()
        for w, h in pages:
            doc.new_page(width=w, height=h)
        doc.save(out_dir / "render.pdf")
        return out_dir / "render.pdf"

    return to_pdf


def test_uncached_docx_chart_is_drawn_alone_on_a_page(tmp_path, monkeypatch):
    path = regions_docx(tmp_path / "memo.docx", chart=EMPTY_CHART)
    seen = []
    # 5 by 3 inches with half inch margins, plus the slack under the chart, in points
    monkeypatch.setattr(render, "available", lambda: True)
    monkeypatch.setattr(render, "to_pdfs", _fake_pdfs())
    monkeypatch.setattr(render, "to_pdf", _fake_pdf([(432, 297)], seen))
    got = office.docx_images(path, Src("memo.docx"))
    got.draw()
    assert "1 chart not read (no cached values)" not in got.needs()
    assert [j.src.cite() for j in got.jobs] == ["memo.docx#para2#img1", "memo.docx#para5#img4"]
    from PIL import Image

    # The crop is the chart itself at the render's 200 dpi
    assert Image.open(io.BytesIO(got.jobs[0].data)).size == (1000, 600)
    (document,) = seen
    assert b'<c:chart r:id="rId1"/>' in document
    assert b"Intro paragraph" not in document
    # The original stays untouched
    assert b"Intro paragraph" in zipfile.ZipFile(path).read("word/document.xml")


def test_chart_pages_put_each_chart_on_its_own_sized_page():
    import xml.etree.ElementTree as ET

    document, clips = ooxml_charts.chart_pages(
        [("rId1", 914400, 914400), ("rId&2", 1828800, 457200)]
    )
    root = ET.fromstring(document)
    sections = root.findall(f".//{{{W}}}sectPr")
    sizes = [
        (s.find(f"{{{W}}}pgSz").get(f"{{{W}}}w"), s.find(f"{{{W}}}pgSz").get(f"{{{W}}}h"))
        for s in sections
    ]
    assert sizes == [("2880", "3060"), ("4320", "2340")]
    assert clips[0] == (0.25, 720 / 3060, 0.75, 2160 / 3060)
    assert [c.get(f"{{{R}}}id") for c in root.iter(f"{{{C}}}chart")] == ["rId1", "rId&2"]


def test_a_render_with_the_wrong_page_count_crops_nothing(tmp_path, monkeypatch):
    path = chart_pptx(tmp_path / "deck.pptx", slides=2)
    _rewrite(path, "ppt/charts/chart1.xml", _drop_data)
    monkeypatch.setattr(render, "available", lambda: True)
    monkeypatch.setattr(render, "to_pdf", _fake_pdf([(720, 540)]))
    got = office.pptx_images(path, Src("deck.pptx"))
    got.draw()
    assert got.jobs == []
    assert got.needs() == ["1 chart not read (no cached values)"]


def test_xlsx_chart_on_a_defined_name_reads_libreoffice_caches(tmp_path, monkeypatch):
    path = chart_xlsx(tmp_path / "book.xlsx")
    # openpyxl writes the chart namespace as the default one
    named = re.compile(rb"<f>[^<]*</f>")
    _rewrite(path, "xl/charts/chart1.xml", lambda d: named.sub(b"<f>Book!Regions</f>", d))
    monkeypatch.setattr(render, "soffice", lambda: None)
    assert "1 chart not read (no cached values)" in sheet.convert(path, Src("book.xlsx")).needs

    def resave(source, target, out_dir, timeout=render.TIMEOUT):
        saved = chart_xlsx(out_dir / f"{source.stem}.xlsx")
        # Where LibreOffice keeps the values it calculated for the chart
        cache = (
            b"<val><numRef><f>Book!Regions</f><numCache><ptCount val='2'/>"
            b"<pt idx='0'><v>100</v></pt><pt idx='1'><v>70</v></pt>"
            b"</numCache></numRef></val>"
        )
        return _rewrite(
            saved,
            "xl/charts/chart1.xml",
            lambda d: re.sub(rb"<val>.*?</val>", cache, d, flags=re.S),
        )

    monkeypatch.setattr(render, "soffice", lambda: "/opt/soffice")
    monkeypatch.setattr(render, "convert_to", resave)
    out = sheet.convert(path, Src("book.xlsx"))
    charts = [b for b in out.blocks if b.text.startswith("Chart")]
    assert [(b.src.cite(), b.text.splitlines()[-1]) for b in charts] == [
        ("book.xlsx#Sales 2026!D2#img1", "| 2 | 70 |")
    ]
    assert out.needs == []


def test_package_is_read_once_across_sniffs_and_convert(tmp_path, monkeypatch):
    from meltify.converters import pick
    from tests.fixtures.make_docs import embedded_docx, embedded_xlsx

    ooxml._package_at.cache_clear()
    calls = []
    real = ooxml.package
    monkeypatch.setattr(ooxml, "package", lambda z: calls.append(z.filename) or real(z))
    draws = []
    real_draw = Embeds.draw
    monkeypatch.setattr(Embeds, "draw", lambda self: draws.append(1) or real_draw(self))
    # No suffix, so the office sniff and then the sheet sniff both look inside
    book = tmp_path / "book"
    embedded_xlsx(tmp_path / "book.xlsx", card("Q3 total")).rename(book)
    kind, convert = pick(book)
    convert(book, Src("book"))
    assert kind == "sheet" and len(calls) == 1
    draws.clear()
    memo = tmp_path / "memo"
    embedded_docx(tmp_path / "memo.docx", card("Q3 total")).rename(memo)
    kind, convert = pick(memo)
    convert(memo, Src("memo"))
    assert kind == "office" and len(calls) == 2 and len(draws) == 1
    # A file rewritten in place is read again
    embedded_docx(memo, card("Q4 total"))
    os.utime(memo, ns=(1, 1))
    ooxml.family(memo)
    assert len(calls) == 3


MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
WPS = "http://schemas.microsoft.com/office/word/2010/wordprocessingShape"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
VML = "urn:schemas-microsoft-com:vml"


def _p(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def word(path: Path, body: str, targets: dict[str, str], members: dict[str, bytes]) -> Path:
    """A docx whose body is `body`, with relationship ids mapped to members under word/"""
    document = (
        f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{W}" xmlns:r="{R}" '
        f'xmlns:a="{A}" xmlns:mc="{MC}" xmlns:wps="{WPS}" xmlns:wp="{WP}" xmlns:v="{VML}">'
        f"<w:body>{body}</w:body></w:document>"
    )
    rels = "".join(_rel(rid, target) for rid, target in targets.items())
    types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/'
        'vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f'<Override PartName="/word/document.xml" ContentType="{DOCX_MAIN}"/></Types>'
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", types)
        z.writestr(
            "_rels/.rels",
            f'<Relationships xmlns="{PKG}">'
            f'<Relationship Id="rId1" Type="{R}/officeDocument" Target="word/document.xml"/>'
            "</Relationships>",
        )
        z.writestr("word/document.xml", document)
        z.writestr(
            "word/_rels/document.xml.rels", f'<Relationships xmlns="{PKG}">{rels}</Relationships>'
        )
        for name, data in members.items():
            z.writestr(f"word/{name}", data)
    return path


def _textbox(text: str, rid: str) -> str:
    """A text box holding a line and a picture, once for Word 2010 and again in VML"""
    choice = (
        "<w:drawing><wp:anchor><a:graphic><a:graphicData><wps:wsp><wps:txbx><w:txbxContent>"
        f'<w:p><w:r><w:t>{text}</w:t></w:r><w:r><w:drawing><a:blip r:embed="{rid}"/>'
        "</w:drawing></w:r></w:p></w:txbxContent></wps:txbx></wps:wsp></a:graphicData>"
        "</a:graphic></wp:anchor></w:drawing>"
    )
    fallback = (
        f"<w:pict><v:shape><v:textbox><w:txbxContent><w:p><w:r><w:t>{text}</w:t></w:r>"
        f'<w:r><w:pict><v:shape><v:imagedata r:id="{rid}"/></v:shape></w:pict></w:r></w:p>'
        "</w:txbxContent></v:textbox></v:shape></w:pict>"
    )
    return (
        f'<w:p><w:r><mc:AlternateContent><mc:Choice Requires="wps">{choice}</mc:Choice>'
        f"<mc:Fallback>{fallback}</mc:Fallback></mc:AlternateContent></w:r></w:p>"
    )


def test_a_picture_shown_many_times_is_read_once_within_one_total(tmp_path, monkeypatch):
    one, two = card("ONE", (300, 100)), card("TWO", (300, 100))
    body = _graphic('<a:blip r:embed="rId1"/>') * 5
    body += _graphic('<a:blip r:embed="rId2"/>') + _graphic('<a:blip r:embed="rId3"/>')
    targets = {"rId1": "media/one.png", "rId2": "media/two.png", "rId3": "media/bomb.bmp"}
    # Zeros deflate far past 100:1, the way a zip bomb does
    members = {"media/one.png": one, "media/two.png": two, "media/bomb.bmp": bytes(2 << 20)}
    path = word(tmp_path / "memo.docx", body, targets, members)
    reads = []
    real = zipfile.ZipFile.read
    monkeypatch.setattr(
        zipfile.ZipFile, "read", lambda z, name, pwd=None: reads.append(name) or real(z, name, pwd)
    )
    # Room for the first picture only
    monkeypatch.setattr("meltify.converters.embeds.MAX_PICTURE_BYTES", len(one) + len(two) // 2)
    got = office.docx_images(path, Src("memo.docx"))
    assert [getattr(n, "filename", n) for n in reads].count("word/media/one.png") == 1
    assert [j.src.cite() for j in got.jobs] == [f"memo.docx#para{n}#img{n}" for n in range(1, 6)]
    # One copy of the bytes behind every place that shows them
    assert all(j.data is got.jobs[0].data for j in got.jobs)
    assert got.needs() == [
        "1 image not read (over the 256 MiB picture total)",
        "1 image not read (expands over 100:1)",
    ]


def test_text_box_pictures_are_read_once_and_paragraphs_count_one_branch(tmp_path):
    pytest.importorskip("markitdown")
    rows = [("A", "B"), ("C", "D")]
    table = "".join(f"<w:tr><w:tc>{_p(a)}</w:tc><w:tc>{_p(b)}</w:tc></w:tr>" for a, b in rows)
    body = _p("Intro") + _textbox("Box text", "rId1") + f"<w:tbl>{table}</w:tbl>"
    body += _p("After") + _graphic('<a:blip r:embed="rId2"/>')
    members = {"media/a.png": card("BOX", (300, 100)), "media/b.png": card("LATER", (300, 100))}
    targets = {"rId1": "media/a.png", "rId2": "media/b.png"}
    path = word(tmp_path / "memo.docx", body, targets, members)
    out = office.convert(path, Src("memo.docx"))
    # The VML copy of the text box neither adds a picture nor shifts the paragraphs after it
    assert [j.src.cite() for j in out.jobs] == ["memo.docx#para3#img1", "memo.docx#para9#img2"]
    # Text cites the same paragraph numbers the pictures do, and a table its first cell
    assert [(b.src.cite(), b.text) for b in out.blocks] == [
        ("memo.docx:1", "1| Intro\n3| Box text\n3| ![](data:image/png;base64...)"),
        ("memo.docx:4", "|  |  |\n| --- | --- |\n| A | B |\n| C | D |"),
        ("memo.docx:8", "8| After"),
    ]


def test_pptx_text_is_cited_by_slide_with_its_charts_after_it(tmp_path):
    pytest.importorskip("markitdown")
    out = office.convert(chart_pptx(tmp_path / "deck.pptx", slides=2), Src("deck.pptx"))
    assert [b.src.cite() for b in out.blocks] == [
        "deck.pptx#slide1",
        "deck.pptx#slide2",
        "deck.pptx#slide2#img1",
    ]
    assert "Slide 1" in out.blocks[0].text and "Slide 2" not in out.blocks[0].text
    assert "Slide 2" in out.blocks[1].text


def test_shallow_keeps_uncached_charts_listed_instead_of_rendering(tmp_path, monkeypatch):

    path = chart_pptx(tmp_path / "deck.pptx")
    _rewrite(path, "ppt/charts/chart1.xml", _drop_data)
    monkeypatch.setattr("meltify.converters.run.current", lambda: RunContext(shallow=True))
    monkeypatch.setattr(render, "available", lambda: True)
    monkeypatch.setattr(render, "to_pdf", lambda *a, **k: pytest.fail("--shallow renders nothing"))
    got = office.pptx_images(path, Src("deck.pptx"))
    assert got.jobs == []
    assert got.needs() == ["1 chart not read (no cached values)"]


def test_utf16_entity_declarations_are_refused(tmp_path):
    bomb = '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE d [<!ENTITY a "x">]><d>&a;</d>'
    path = tmp_path / "a.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("le.xml", bomb.encode("utf-16"))
        z.writestr("be.xml", bomb.encode("utf-16-be"))
    with zipfile.ZipFile(path) as z:
        for part in ("le.xml", "be.xml"):
            with pytest.raises(ValueError, match="ENTITY"):
                xmlsafe.zip_xml(z, part)


def test_shallow_never_asks_libreoffice_to_recalculate(tmp_path, monkeypatch):

    monkeypatch.setattr("meltify.converters.run.current", lambda: RunContext(shallow=True))
    monkeypatch.setattr(render, "soffice", lambda: "/opt/soffice")
    monkeypatch.setattr(render, "convert_to", lambda *a, **k: pytest.fail("started soffice"))
    out = sheet.convert(formula_xlsx(tmp_path / "book.xlsx"), Src("book.xlsx"))
    assert out.needs == ["1 chart not read (no cached values)"]


@pytest.mark.parametrize("why", ["part limit", "expands over 100:1"])
def test_oversized_or_inflating_main_part_is_refused_before_any_reader(tmp_path, monkeypatch, why):
    # Spaces deflate far past 100:1, the way a zip bomb does
    padding = " " * (2 << 20) if why.startswith("expands") else _p("x" * 3000)
    path = word(tmp_path / "memo.docx", _p("Intro") + padding, {}, {})
    if why == "part limit":
        # Over the main part's size, while the package parts around it still fit
        monkeypatch.setattr(limits, "MAX_PART_BYTES", 2000)
    with pytest.raises(ValueError, match=why):
        office.convert(path, Src("memo.docx"))
    with zipfile.ZipFile(path) as z, pytest.raises(ValueError, match=why):
        xmlsafe.zip_xml(z, "word/document.xml")


def test_word_text_is_read_without_unpacking_its_pictures(tmp_path, monkeypatch):
    import base64

    pytest.importorskip("markitdown")
    big = card("BIG PICTURE", (1200, 800))
    # mammoth draws VML pictures, so this one lands in the text
    picture = '<w:p><w:r><w:pict><v:shape><v:imagedata r:id="rId1"/></v:shape></w:pict></w:r></w:p>'
    body = _p("Intro") + picture + _p("After")
    path = word(tmp_path / "memo.docx", body, {"rId1": "media/big.png"}, {"media/big.png": big})
    encoded = []
    real = base64.b64encode
    monkeypatch.setattr(
        base64, "b64encode", lambda data, *a: encoded.append(len(data)) or real(data, *a)
    )
    out = office.convert(path, Src("memo.docx"))
    # mammoth would base64 the whole picture into a data URI that markitdown cuts anyway
    assert max(encoded, default=0) < 1000
    assert [(b.src.cite(), b.text) for b in out.blocks] == [
        ("memo.docx:1", "1| Intro\n2| ![](data:image/png;base64...)\n3| After")
    ]
    assert [j.data for j in out.jobs] == [big]


def test_markitdown_runs_without_its_file_type_guessing(tmp_path, monkeypatch):
    markitdown = pytest.importorskip("markitdown")
    monkeypatch.setattr(markitdown, "MarkItDown", lambda *a, **k: pytest.fail("built MarkItDown"))
    docx = word(tmp_path / "memo.docx", _p("Intro"), {}, {})
    assert office.convert(docx, Src("memo.docx")).blocks[0].text == "1| Intro"
    deck = office.convert(chart_pptx(tmp_path / "deck.pptx"), Src("deck.pptx"))
    assert deck.blocks[0].src.cite() == "deck.pptx#slide1"


def _spy_rows(monkeypatch) -> list[str]:
    asked: list[str] = []
    real = sheet.rows_by_title

    def spy(source):
        rows = real(source)
        return lambda title: asked.append(title) or rows(title)

    monkeypatch.setattr(sheet, "rows_by_title", spy)
    return asked


def test_sheet_rows_are_kept_only_for_sheets_a_chart_formula_names(tmp_path, monkeypatch):
    from openpyxl import load_workbook

    path = chart_xlsx(tmp_path / "book.xlsx")
    wb = load_workbook(path)
    wb.create_sheet("Other").append(["not", "charted"])
    wb.save(path)
    asked = _spy_rows(monkeypatch)
    out = sheet.convert(path, Src("book.xlsx"))
    assert set(asked) == {"Sales 2026"}
    assert any("| 1 | not | charted |" in b.text for b in out.blocks)
    plain = tmp_path / "plain.xlsx"
    wb.remove(wb["Sales 2026"])
    wb.save(plain)
    asked.clear()
    sheet.convert(plain, Src("plain.xlsx"))
    assert asked == []


def awkward_xlsx(path: Path, error: bool = False) -> Path:
    from datetime import date, datetime, time

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Mixed"
    ws.append([None, 42, 3.5, 1.0, True, date(2026, 1, 2), datetime(2026, 1, 2, 13, 45)])
    ws.append([time(9, 30), "a|b\nc", "=B1*2", 12345678901234, -0.000123, 1e20])
    if error:
        ws["C3"] = "#N/A"
    ws.sheet_state = "visible"
    # Data starting away from A1 keeps its row numbers and column letters
    offset = wb.create_sheet("Offset")
    offset["D5"], offset["F7"] = "first", 7
    # calamine panics on the rows of a sheet with no cells
    wb.create_sheet("Empty")
    hidden = wb.create_sheet("Old")
    hidden.append(["x"])
    hidden.sheet_state = "veryHidden"
    wb.save(path)
    return path


@pytest.mark.parametrize("error", [False, True])
def test_calamine_and_openpyxl_melt_cells_alike(tmp_path, monkeypatch, error):
    import importlib.util

    pytest.importorskip("python_calamine")
    path = awkward_xlsx(tmp_path / "book.xlsx", error)
    fast = sheet.convert(path, Src("book.xlsx"))
    real = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda n, *a: None if n == "python_calamine" else real(n, *a)
    )
    slow = sheet.convert(path, Src("book.xlsx"))
    assert [(b.src.cite(), b.text) for b in fast.blocks] == [
        (b.src.cite(), b.text) for b in slow.blocks
    ]
    assert fast.needs == slow.needs == ["hidden sheets Old"]
    # An error value reads as blank in calamine, so that workbook goes to openpyxl
    assert ("#N/A" in fast.blocks[0].text) == error


def test_xlsx_cells_skip_openpyxl_when_calamine_is_installed(tmp_path, monkeypatch):
    import openpyxl

    pytest.importorskip("python_calamine")
    path = awkward_xlsx(tmp_path / "book.xlsx")
    monkeypatch.setattr(openpyxl, "load_workbook", lambda *a, **k: pytest.fail("used openpyxl"))
    out = sheet.convert(path, Src("book.xlsx"))
    assert "| 1 |  | 42 | 3.5 | 1 | True | 2026-01-02 | 2026-01-02 13:45 |" in out.blocks[0].text
