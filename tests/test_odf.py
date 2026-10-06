import base64
import json
import shutil
import zipfile
from pathlib import Path

import pytest

from meltify.cli import main
from meltify.converters.odf import MAX_REPEAT, convert
from meltify.evidence import Src
from tests.fixtures.make_docs import card

SAMPLES = Path(__file__).parent / "fixtures" / "samples" / "docs"
NS = (
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
    'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
    'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
    'xmlns:presentation="urn:oasis:names:tc:opendocument:xmlns:presentation:1.0" '
    'xmlns:xlink="http://www.w3.org/1999/xlink"'
)
PICTURE = card("INVOICE 2026", (300, 100))


def _odf(path: Path, body: str, prolog: str = "", members: dict[str, bytes] | None = None) -> Path:
    xml = f'<?xml version="1.0"?>{prolog}<office:document-content {NS}><office:body>{body}'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("content.xml", xml + "</office:body></office:document-content>")
        for name, data in (members or {}).items():
            z.writestr(name, data)
    return path


def _frame(href: str) -> str:
    return f'<draw:frame><draw:image xlink:href="{href}"/></draw:frame>'


def _melt(path: Path):
    return convert(path, Src(path.name))


def test_odt_headings_and_paragraphs_cite_their_position(tmp_path):
    path = _odf(
        tmp_path / "a.odt",
        "<office:text><text:p/>"
        '<text:h text:outline-level="2">계약 개요</text:h>'
        '<text:p>금액은<text:s text:c="2"/>300만원 '
        "<text:hidden-text>secret</text:hidden-text></text:p>"
        '<table:table table:name="T1"><table:table-row>'
        "<table:table-cell><text:p>a|b</text:p></table:table-cell>"
        "</table:table-row></table:table>"
        "<text:list><text:list-item><text:p>항목 하나</text:p></text:list-item></text:list>"
        "</office:text>",
    )
    out = _melt(path)
    assert [(b.src.cite(), b.text) for b in out.blocks] == [
        ("a.odt:2", "2| ## 계약 개요\n3| 금액은  300만원\n4| 항목 하나"),
        ("a.odt#T1", "| row | A |\n|---|---|\n| 1 | a\\|b |"),
    ]


def test_ods_expands_repeats_and_caps_them(tmp_path):
    path = _odf(
        tmp_path / "a.ods",
        '<office:spreadsheet><table:table table:name="Sheet">'
        '<table:table-row><table:table-cell table:number-columns-repeated="2"/>'
        "<table:table-cell><text:p>C1</text:p></table:table-cell></table:table-row>"
        '<table:table-row table:number-rows-repeated="1048570">'
        '<table:table-cell table:number-columns-repeated="16384"/></table:table-row>'
        '<table:table-row table:number-rows-repeated="5000">'
        "<table:table-cell><text:p>x</text:p></table:table-cell></table:table-row>"
        "</table:table></office:spreadsheet>",
    )
    out = _melt(path)
    (block,) = out.blocks
    assert block.src.cite() == "a.ods#Sheet"
    lines = block.text.splitlines()
    assert lines[2] == "| 1 |  |  | C1 |"
    # Empty padding still advances the row count, so the x rows start right after it
    assert lines[3] == "| 1048572 | x |  |  |"
    assert len(lines) == 3 + MAX_REPEAT
    assert out.needs == [f"repeated rows cut at {MAX_REPEAT}"]


def test_real_ods_with_million_padding_rows():
    out = _melt(SAMPLES / "odftoolkit-tableRepeated.ods")
    (block,) = out.blocks
    assert block.src.cite() == "odftoolkit-tableRepeated.ods#Sheet1"
    assert block.text.splitlines()[-1].startswith("| 26 |")
    assert block.text.splitlines()[-1].endswith("| dd |")


def test_odp_slides_and_speaker_notes(tmp_path):
    path = _odf(
        tmp_path / "a.odp",
        "<office:presentation>"
        "<draw:page><draw:frame><draw:text-box/></draw:frame></draw:page>"
        "<draw:page><draw:frame><draw:text-box><text:p>매출 120억</text:p></draw:text-box>"
        "</draw:frame><presentation:notes><draw:frame><draw:text-box>"
        "<text:p>발표자 메모</text:p></draw:text-box></draw:frame></presentation:notes>"
        "</draw:page></office:presentation>",
    )
    (block,) = _melt(path).blocks
    assert block.src.cite() == "a.odp#slide2"
    assert block.text == "매출 120억\n\nNotes:\n발표자 메모"


def test_real_odt_and_odp():
    odt = _melt(SAMPLES / "odftoolkit-HelloWorld.odt").blocks
    assert odt[0].src.cite() == "odftoolkit-HelloWorld.odt:1"
    assert odt[0].text.splitlines()[-1] == "5| Hello World5"
    odp = _melt(SAMPLES / "odftoolkit-SlideTest2.odp").blocks
    assert odp[0].src.cite() == "odftoolkit-SlideTest2.odp#slide1"
    # Pretty-printed spans collapse to single spaces like the slide shows them
    assert "The ODF Toolkit provides a home for libraries" in odp[0].text


def test_entity_declarations_are_refused(tmp_path):
    path = _odf(
        tmp_path / "a.odt",
        "<office:text><text:p>&a;</text:p></office:text>",
        prolog='<!DOCTYPE d [<!ENTITY a "boom">]>',
    )
    with pytest.raises(ValueError, match="ENTITY"):
        _melt(path)


def test_read_cites_odf_end_to_end(tmp_path, monkeypatch, capsys):
    shutil.copy(SAMPLES / "odftoolkit-tableRepeated.ods", tmp_path / "t.ods")
    monkeypatch.chdir(tmp_path)
    main(["read", "t.ods", "--json", "--limit", "0"])
    (row,) = json.loads(capsys.readouterr().out)["results"]
    assert row["kind"] == "odf"
    assert "## t.ods#Sheet1\n| row |" in Path(row["out"]).read_text()


def test_odt_pictures_cite_their_paragraph(tmp_path):
    path = _odf(
        tmp_path / "a.odt",
        "<office:text><text:p>Intro</text:p>"
        f"<text:p>See {_frame('Pictures/a.png')}</text:p>"
        # LibreOffice pairs an SVG with a PNG fallback, and only the PNG is read
        '<text:p><draw:frame><draw:image xlink:href="Pictures/b.svg"/>'
        '<draw:image xlink:href="./Pictures/a.png"/></draw:frame></text:p>'
        f"<text:p>{_frame('http://example.org/x.png')}{_frame('../outside.png')}</text:p>"
        f"<text:p>{_frame('Pictures/gone.png')}{_frame('Pictures/c.svm')}</text:p>"
        '<text:p><draw:frame><draw:object xlink:href="./Object 1"/></draw:frame></text:p>'
        f"<text:tracked-changes>{_frame('Pictures/a.png')}</text:tracked-changes>"
        f'<table:table table:name="T"><table:table-row><table:table-cell>'
        f"<text:p>{_frame('Pictures/a.png')}</text:p></table:table-cell></table:table-row>"
        "</table:table></office:text>",
        members={"Pictures/a.png": PICTURE, "Pictures/b.svg": b"<svg/>", "Pictures/c.svm": b"x"},
    )
    out = _melt(path)
    assert [j.src.cite() for j in out.jobs] == [
        "a.odt#para2#img1",
        "a.odt#para3#img2",
        "a.odt#img8",
    ]
    assert out.jobs[0].data == PICTURE
    assert sorted(out.needs) == [
        "1 embedded object not read",
        "1 missing image not read",
        "1 svm image not read",
        "2 linked images not read",
    ]


def test_ods_and_odp_pictures_cite_sheet_and_slide(tmp_path):
    ods = _odf(
        tmp_path / "a.ods",
        '<office:spreadsheet><table:table table:name="Sales"><table:shapes>'
        f"{_frame('Pictures/a.png')}</table:shapes></table:table></office:spreadsheet>",
        members={"Pictures/a.png": PICTURE},
    )
    assert [j.src.cite() for j in _melt(ods).jobs] == ["a.ods#Sales#img1"]
    odp = _odf(
        tmp_path / "a.odp",
        "<office:presentation><draw:page/>"
        f"<draw:page>{_frame('Pictures/a.png')}{_frame('Pictures/a.png')}</draw:page>"
        "</office:presentation>",
        members={"Pictures/a.png": PICTURE},
    )
    assert [j.src.cite() for j in _melt(odp).jobs] == ["a.odp#slide2#img1", "a.odp#slide2#img2"]


def test_odg_drawing_reads_text_and_pictures_by_page(tmp_path):
    inline = base64.b64encode(PICTURE).decode()
    path = _odf(
        tmp_path / "a.odg",
        "<office:drawing><draw:page><draw:custom-shape><text:p>Floor plan</text:p>"
        "</draw:custom-shape></draw:page><draw:page>"
        f"<draw:frame><draw:image><office:binary-data>{inline}</office:binary-data>"
        "</draw:image></draw:frame></draw:page></office:drawing>",
    )
    out = _melt(path)
    assert [(b.src.cite(), b.text) for b in out.blocks] == [("a.odg#p1", "Floor plan")]
    assert [(j.src.cite(), j.data) for j in out.jobs] == [("a.odg#p2#img1", PICTURE)]


CHART_NS = (
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
    'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
    'xmlns:chart="urn:oasis:names:tc:opendocument:xmlns:chart:1.0"'
)


def _chart_object(rows: str) -> bytes:
    """A chart sub-document the way LibreOffice writes it, with its own local table"""
    return (
        f"<office:document-content {CHART_NS}><office:body><office:chart>"
        "<chart:chart><chart:title><text:p>Revenue PAPAYA</text:p></chart:title>"
        "<chart:plot-area><chart:axis chart:dimension='x'>"
        "<chart:title><text:p>Quarter</text:p></chart:title></chart:axis>"
        "<chart:series chart:values-cell-range-address='local-table.$B$2:.$B$3'/>"
        "</chart:plot-area>"
        "<table:table table:name='local-table'><table:table-header-rows><table:table-row>"
        "<table:table-cell/><table:table-cell><text:p>Sales</text:p></table:table-cell>"
        f"</table:table-row></table:table-header-rows><table:table-rows>{rows}"
        "</table:table-rows></table:table></chart:chart></office:chart></office:body>"
        "</office:document-content>"
    ).encode()


def _row(*cells: str) -> str:
    inner = "".join(f"<table:table-cell><text:p>{c}</text:p></table:table-cell>" for c in cells)
    return f"<table:table-row>{inner}</table:table-row>"


def test_odf_chart_reads_its_local_table(tmp_path):
    frame = '<draw:frame><draw:object xlink:href="./Object 1"/>{}</draw:frame>'
    replacement = '<draw:image xlink:href="./ObjectReplacements/Object 1"/>'
    path = _odf(
        tmp_path / "chart.odt",
        "<office:text><text:p>Quarterly report body</text:p>"
        f"<text:p>{frame.format(replacement)}</text:p>"
        f'<text:p><draw:frame><draw:object xlink:href="./Object 2"/></draw:frame></text:p>'
        f'<text:p><draw:frame><draw:object xlink:href="./Object 3"/></draw:frame></text:p>'
        "</office:text>",
        members={
            "Object 1/content.xml": _chart_object(_row("Q1", "123") + _row("Q2", "456")),
            "Object 2/content.xml": _chart_object(""),
            "Object 3/content.xml": f"<office:document-content {CHART_NS}/>".encode(),
        },
    )
    out = _melt(path)
    blocks = {b.src.cite(): b.text for b in out.blocks}
    # The replacement picture beside the object is the same chart, so it isn't read twice
    assert blocks["chart.odt#para2#img1"] == (
        "Chart: Revenue PAPAYA\nAxes: Quarter\n| category | Sales |\n|---|---|\n"
        "| Q1 | 123 |\n| Q2 | 456 |"
    )
    assert out.jobs == []
    assert sorted(out.needs) == [
        "1 chart not read (no cached values)",
        "1 embedded object not read",
    ]


def test_charts_sit_with_their_slide_sheet_and_paragraph(tmp_path):
    chart = _chart_object(_row("Q1", "123"))
    obj = '<draw:frame><draw:object xlink:href="./Object 1"/></draw:frame>'
    odp = _odf(
        tmp_path / "deck.odp",
        f"<office:presentation><draw:page>{obj}<draw:frame><draw:text-box><text:p>first</text:p>"
        "</draw:text-box></draw:frame></draw:page><draw:page><draw:frame><draw:text-box>"
        "<text:p>second</text:p></draw:text-box></draw:frame></draw:page></office:presentation>",
        members={"Object 1/content.xml": chart},
    )
    assert [b.src.cite() for b in _melt(odp).blocks] == [
        "deck.odp#slide1",
        "deck.odp#slide1#img1",
        "deck.odp#slide2",
    ]
    lines = "".join(f"<text:p>line {n}</text:p>" for n in range(1, 251))
    odt = _odf(
        tmp_path / "long.odt",
        f"<office:text>{lines}<text:p>{obj}</text:p>{lines}</office:text>",
        members={"Object 1/content.xml": chart},
    )
    # Text blocks hold 200 lines each, so the chart in paragraph 251 lands between them
    assert [b.src.cite() for b in _melt(odt).blocks] == [
        "long.odt:1",
        "long.odt:201",
        "long.odt#para251#img1",
        "long.odt:402",
    ]
