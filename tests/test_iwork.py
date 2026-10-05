import importlib.util
import json
import shutil
import zipfile
from pathlib import Path

import pytest

from meltify.cli import main
from meltify.converters import iwork
from meltify.converters.iwork import convert
from meltify.evidence import Src

SAMPLES = Path(__file__).parent / "fixtures" / "samples" / "docs"
JPEG = b"\xff\xd8\xff\xe0 fake preview"


def _melt(path: Path):
    return convert(path, Src(path.name))


def test_numbers_cells_cite_sheet_and_table():
    pytest.importorskip("numbers_parser")
    out = _melt(SAMPLES / "numbers-parser-issue-18.numbers")
    (block,) = out.blocks
    assert block.src.cite() == "numbers-parser-issue-18.numbers#Sheet 1>Table 1"
    lines = block.text.splitlines()
    assert lines[0] == "| row | A | B | C | D | E |"
    # B3:D3 is merged, so C3 and D3 stay blank instead of repeating it
    assert lines[4] == "| 3 | A3 | B3:D3 |  |  |  |"
    assert not out.jobs and not out.needs


def test_encrypted_numbers_is_reported():
    pytest.importorskip("numbers_parser")
    out = _melt(SAMPLES / "numbers-parser-encrypted.numbers")
    assert out.needs == ["iwork encrypted"] and not out.blocks


def test_numbers_without_extra_falls_back_to_preview(monkeypatch):
    real = importlib.util.find_spec

    def find_spec(name, *args):
        return None if name == "numbers_parser" else real(name, *args)

    monkeypatch.setattr(iwork.importlib.util, "find_spec", find_spec)
    path = SAMPLES / "numbers-parser-issue-18.numbers"
    with zipfile.ZipFile(path) as z:
        has_preview = "preview.jpg" in z.namelist()
    out = _melt(path)
    assert out.needs[0] == "iwork extra"
    assert out.needs[1:] == (["iwork preview only"] if has_preview else ["iwork preview missing"])


def test_keynote_preview_becomes_an_image_job():
    out = _melt(SAMPLES / "keynote-parser-table.key")
    (job,) = out.jobs
    assert job.kind == "image"
    assert job.src.cite() == "keynote-parser-table.key#att=preview.jpg"
    assert job.data.startswith(b"\xff\xd8\xff")
    assert out.needs == ["iwork preview only"]


def test_older_pages_uses_quicklook_preview(tmp_path):
    path = tmp_path / "a.pages"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Index/Document.iwa", b"")
        z.writestr("QuickLook/Thumbnail.jpg", b"\xff\xd8\xff small")
        z.writestr("QuickLook/Preview.jpg", JPEG)
    (job,) = _melt(path).jobs
    assert job.src.cite() == "a.pages#att=QuickLook/Preview.jpg"
    assert job.data == JPEG


def test_pages_without_preview_says_so(tmp_path):
    path = tmp_path / "a.pages"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Index/Document.iwa", b"")
    out = _melt(path)
    assert out.needs == ["iwork preview missing"] and not out.jobs


def test_read_cites_numbers_end_to_end(tmp_path, monkeypatch, capsys):
    pytest.importorskip("numbers_parser")
    shutil.copy(SAMPLES / "numbers-parser-issue-18.numbers", tmp_path / "n.numbers")
    monkeypatch.chdir(tmp_path)
    main(["read", "n.numbers", "--json", "--limit", "0"])
    (row,) = json.loads(capsys.readouterr().out)["results"]
    assert row["kind"] == "iwork"
    assert "## n.numbers#Sheet 1>Table 1\n" in Path(row["out"]).read_text()
