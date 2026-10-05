import json
import shutil
import zipfile
from pathlib import Path

import pytest

from meltify.cli import main
from meltify.converters.odf import MAX_REPEAT, convert
from meltify.evidence import Src

SAMPLES = Path(__file__).parent / "fixtures" / "samples" / "docs"
NS = (
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
    'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
    'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
    'xmlns:presentation="urn:oasis:names:tc:opendocument:xmlns:presentation:1.0"'
)


def _odf(path: Path, body: str, prolog: str = "") -> Path:
    xml = f'<?xml version="1.0"?>{prolog}<office:document-content {NS}><office:body>{body}'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("content.xml", xml + "</office:body></office:document-content>")
    return path


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
