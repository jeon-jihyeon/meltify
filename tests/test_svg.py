import json
from pathlib import Path

import pytest

from meltify.cli import main
from meltify.converters.svg import convert
from meltify.evidence import Src

CHART = """<svg xmlns="http://www.w3.org/2000/svg">
<title>분기 매출</title>
<desc>막대 차트</desc>
<g id="bars">
  <text id="t1" x="10" y="20">매출 <tspan font-weight="bold">120억</tspan></text>
  <text x="10" y="40">영업이익 8억</text>
</g>
<text x="5" y="50" display="none">숨김 하나</text>
<g style="visibility: hidden"><text>숨김 둘</text><text visibility="visible">다시 보임</text></g>
<text opacity="0">숨김 셋</text>
</svg>
"""


def _melt(tmp_path: Path, xml: str):
    path = tmp_path / "a.svg"
    path.write_text(xml, encoding="utf-8")
    return convert(path, Src("a.svg"))


def test_text_title_and_desc_cite_by_id_or_line(tmp_path):
    out = _melt(tmp_path, CHART)
    assert [(b.src.cite(), b.text) for b in out.blocks] == [
        ("a.svg:2", "2| title: 분기 매출\n3| desc: 막대 차트"),
        ("a.svg#t1:5", "매출 120억"),
        (
            "a.svg:6",
            " 6| 영업이익 8억\n 8| [hidden] 숨김 하나\n 9| [hidden] 숨김 둘\n 9| 다시 보임\n"
            "10| [hidden] 숨김 셋",
        ),
    ]
    assert out.hidden == 3
    assert out.needs == ["hidden 3 texts"]


@pytest.mark.parametrize(
    "xml",
    [
        '<!DOCTYPE s [<!ENTITY x SYSTEM "file:///etc/passwd">]><svg><text>&x;</text></svg>',
        '<!DOCTYPE l [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><svg><text>&b;</text></svg>',
    ],
)
def test_entity_declarations_are_refused(tmp_path, xml):
    with pytest.raises(ValueError, match="ENTITY"):
        _melt(tmp_path, xml)


def test_utf16_entity_declaration_is_refused(tmp_path):
    path = tmp_path / "a.svg"
    path.write_bytes('<!DOCTYPE s [<!ENTITY x "y">]><svg/>'.encode("utf-16"))
    with pytest.raises(ValueError, match="ENTITY"):
        convert(path, Src("a.svg"))


def test_read_cites_svg_end_to_end(tmp_path, monkeypatch, capsys):
    (tmp_path / "a.svg").write_text(CHART, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    main(["read", "a.svg", "--json", "--limit", "0"])
    (row,) = json.loads(capsys.readouterr().out)["results"]
    assert row["needs"] == ["hidden 3 texts"]
    md = Path(row["out"]).read_text()
    assert "## a.svg#t1:5\n매출 120억" in md
