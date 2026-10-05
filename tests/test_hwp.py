import io
import json
import shutil
from pathlib import Path

from meltify.cli import main
from meltify.converters import pick
from meltify.converters.hwp import convert
from meltify.evidence import Src

SAMPLES = Path(__file__).parent / "fixtures" / "samples" / "docs"


def _melt(name: str):
    path = SAMPLES / name
    return convert(path, Src(name))


def test_hwp_table_cites_section_and_paragraph():
    out = _melt("hwplib-table.hwp")
    md = out.markdown("t")
    assert "## hwplib-table.hwp#s1:1\n| ABC 123 | DEF | GHI |\n|---|---|---|" in md
    assert "| UVM | 123 | 456 |" in md
    # The second table has only blank cells, so it adds nothing to quote
    assert len(out.blocks) == 1


def test_hwpx_merged_cells_show_once_at_their_anchor():
    md = _melt("hwpxlib-SimpleTable.hwpx").markdown("t")
    assert "| 1 |  | 2 |\n|---|---|---|\n|  |  | 3 |\n| 5 | 4 |  |" in md


def test_hwpx_paragraphs_are_numbered_by_position():
    out = _melt("hwpxlib-sample1.hwpx")
    assert out.blocks[0].src.cite() == "hwpxlib-sample1.hwpx#s1:1"
    assert out.blocks[0].text.splitlines()[0] == "1| 수학"


def test_hwp_picture_becomes_one_image_job():
    out = _melt("hwplib-picture.hwp")
    # The sample draws one picture four times, and only the first placement is OCR'd
    assert [j.src.cite() for j in out.jobs] == ["hwplib-picture.hwp#s1#img1"]
    assert out.jobs[0].kind == "image"
    assert out.jobs[0].data.startswith(b"\x89PNG")


def test_distribution_hwp_is_reported_as_encrypted():
    out = _melt("hwplib-distribution.hwp")
    assert out.needs == ["hwp encrypted"] and not out.blocks


def test_generated_hwpx_keeps_empty_paragraphs_in_the_count(tmp_path):
    from hwpx import HwpxDocument

    doc = HwpxDocument.new()
    doc.paragraphs[0].text = "첫 줄"
    doc.add_paragraph("")
    table = doc.add_table(2, 2)
    table.set_cell_text(0, 0, "키")
    table.set_cell_text(1, 1, "값|x")
    doc.add_paragraph("끝 문단")
    buf = io.BytesIO()
    doc.save_to_stream(buf)
    path = tmp_path / "g.hwpx"
    path.write_bytes(buf.getvalue())

    cites = [(b.src.cite(), b.text) for b in convert(path, Src("g.hwpx")).blocks]
    assert cites == [
        ("g.hwpx#s1:1", "1| 첫 줄"),
        ("g.hwpx#s1:3", "| 키 |  |\n|---|---|\n|  | 값\\|x |"),
        ("g.hwpx#s1:4", "4| 끝 문단"),
    ]


def test_read_cites_hwp_end_to_end(tmp_path, monkeypatch, capsys):
    for name in ("hwplib-table.hwp", "hwplib-distribution.hwp"):
        shutil.copy(SAMPLES / name, tmp_path / name)
    assert pick(tmp_path / "a.hwpx")[0] == "hwp"
    monkeypatch.chdir(tmp_path)
    main(["read", "hwplib-table.hwp", "hwplib-distribution.hwp", "--json", "--limit", "0"])
    rows = {r["cite"]: r for r in json.loads(capsys.readouterr().out)["results"]}
    md = Path(rows["hwplib-table.hwp"]["out"]).read_text()
    assert "## hwplib-table.hwp#s1:1\n" in md
    assert rows["hwplib-distribution.hwp"]["needs"] == ["hwp encrypted"]
