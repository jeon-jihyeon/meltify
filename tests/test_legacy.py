import json
import sys
from pathlib import Path

import pytest

from meltify.cli import main
from meltify.converters import legacy
from meltify.evidence import Src

SAMPLES = Path(__file__).parent / "fixtures" / "samples" / "legacy"
DOC = SAMPLES / "SampleDoc.doc"
XLS = SAMPLES / "SampleSS.xls"
PPT = SAMPLES / "basic_test_ppt_file.ppt"


def _rows(tmp_path, monkeypatch, capsys, *paths):
    monkeypatch.chdir(tmp_path)
    main(["read", *map(str, paths), "--json", "--limit", "0"])
    out = json.loads(capsys.readouterr().out)
    return {r["cite"]: r for r in out["results"]}


def _md(row):
    return Path(row["out"]).read_text(encoding="utf-8")


def test_doc_cites_lines(tmp_path, monkeypatch, capsys):
    pytest.importorskip("legacy_doc")
    row = _rows(tmp_path, monkeypatch, capsys, DOC)[str(DOC)]
    assert row["kind"] == "legacy" and row["needs"] == []
    md = _md(row)
    assert f"## {DOC}:1\n1| I am a test document" in md
    assert "4| This is page two" in md


def test_xls_cites_sheets_like_xlsx(tmp_path, monkeypatch, capsys):
    pytest.importorskip("python_calamine")
    md = _md(_rows(tmp_path, monkeypatch, capsys, XLS)[str(XLS)])
    assert f"## {XLS}#First Sheet\n| row | A | B |" in md
    # Numbers come back as ints, so B7 reads 10 like openpyxl would show it
    assert f"## {XLS}#Sheet Number 2" in md
    assert "| 7 | 1 | 10 | 2 | 13 |" in md
    assert "| 3 |" not in md


def test_ppt_pages_cite_the_ppt(tmp_path, monkeypatch, capsys):
    if legacy.soffice() is None:
        pytest.skip("LibreOffice isn't installed")
    row = _rows(tmp_path, monkeypatch, capsys, PPT)[str(PPT)]
    assert f"## {PPT}#p2\n" in _md(row)
    assert "This is a test title" in _md(row)


def test_ppt_without_soffice_says_what_to_install(monkeypatch):
    monkeypatch.setattr(legacy, "soffice", lambda: None)
    out = legacy.convert(PPT, Src(str(PPT)))
    assert out.blocks == [] and out.needs == ["ppt (install LibreOffice)"]


def test_doc_without_legacy_doc_or_soffice_reports_the_extra(tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "legacy_doc", None)
    monkeypatch.setattr(legacy, "soffice", lambda: None)
    row = _rows(tmp_path, monkeypatch, capsys, DOC)[str(DOC)]
    assert row["needs"] == ["legacy-doc"]
    assert row["hint"] == "meltify doctor --install office"


def test_unreadable_doc_points_at_libreoffice(tmp_path, monkeypatch):
    pytest.importorskip("legacy_doc")
    monkeypatch.setattr(legacy, "soffice", lambda: None)
    bad = tmp_path / "broken.doc"
    bad.write_bytes(b"not an OLE file")
    out = legacy.convert(bad, Src(str(bad)))
    assert out.blocks == []
    assert len(out.needs) == 1 and out.needs[0].endswith("install LibreOffice to retry)")
