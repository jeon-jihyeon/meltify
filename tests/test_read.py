import json
from pathlib import Path

from meltify.cli import main
from tests.fixtures.make_docs import mixed_folder, nested_mail, tricky_mail
from tests.fixtures.make_pdf import hidden_pdf


def _rows(tmp_path, monkeypatch, capsys, *paths):
    monkeypatch.chdir(tmp_path)
    code = main(["read", *map(str, paths), "--json", "--limit", "0"])
    return code, json.loads(capsys.readouterr().out)


def test_mixed_folder_melts_with_citations(tmp_path, monkeypatch, capsys):
    folder = mixed_folder(tmp_path / "in")
    code, out = _rows(tmp_path, monkeypatch, capsys, folder)
    assert code == 0
    by_cite = {r["cite"]: r for r in out["results"]}

    pdf = by_cite[f"{folder}/report.pdf"]
    assert pdf["kind"] == "pdf" and pdf["needs"] == ["ocr pages 2"]
    md = Path(pdf["out"]).read_text()
    assert f"## {folder}/report.pdf#p1\nPage one says the deadline is Friday" in md

    sheet = Path(by_cite[f"{folder}/calendar.xlsx"]["out"]).read_text()
    assert f"## {folder}/calendar.xlsx#Calendar" in sheet
    assert "| 7 |  | budget review |" in sheet
    assert by_cite[f"{folder}/calendar.xlsx"]["needs"] == ["hidden sheets Old"]

    notes = Path(by_cite[f"{folder}/notes.txt"]["out"]).read_text()
    assert "2| 둘째 줄 한글" in notes
    assert "완료 보고" in Path(by_cite[f"{folder}/legacy.txt"]["out"]).read_text()

    assert by_cite[f"{folder}/scan.png"]["needs"] == ["ocr"]
    assert by_cite[f"{folder}/blob.bin"]["needs"] == ["unsupported format"]

    mail = f"{folder}/mail/handover.eml"
    assert "Attachments: cal.xlsx" in Path(by_cite[mail]["out"]).read_text()
    att = by_cite[f"{mail}#att=cal.xlsx"]
    assert att["kind"] == "sheet"
    assert f"## {mail}#att=cal.xlsx#Calendar" in Path(att["out"]).read_text()

    assert not any(".hidden" in c for c in by_cite)
    index = Path(out["artifacts"][0]["path"])
    assert len(index.read_text().splitlines()) == len(out["results"])


def test_pdf_reports_hidden_spans(tmp_path, monkeypatch, capsys):
    pdf = hidden_pdf(tmp_path / "h.pdf")
    _, out = _rows(tmp_path, monkeypatch, capsys, pdf)
    assert out["results"][0]["hidden"] == 5
    assert "hidden 5 spans" in out["results"][0]["needs"]


def test_office_without_markitdown_reports_install_hint(tmp_path, monkeypatch, capsys):
    import importlib.util

    real = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda n, *a: None if n == "markitdown" else real(n, *a)
    )
    (tmp_path / "a.docx").write_bytes(b"PK")
    code, out = _rows(tmp_path, monkeypatch, capsys, tmp_path / "a.docx")
    assert code == 0
    row = out["results"][0]
    assert row["hint"] == "meltify doctor --install office"
    assert out["warnings"]


def test_attachment_names_stay_inside_out_dir_and_do_not_overwrite(tmp_path, monkeypatch, capsys):
    mail = tricky_mail(tmp_path / "t.eml", tmp_path / "abs" / "absolute.txt")
    code, out = _rows(tmp_path, monkeypatch, capsys, mail)
    assert code == 0
    out_dir = (tmp_path / "meltify-out").resolve()
    written = [Path(r["out"]).resolve() for r in out["results"]]
    assert len(set(written)) == len(written) == 6
    assert all(w.is_relative_to(out_dir) for w in written)
    assert not (tmp_path / "escape.txt").exists() and not (tmp_path / "abs").exists()
    texts = sorted(w.read_text() for w in written)
    assert sum("first copy" in t for t in texts) == 1
    assert sum("second copy" in t for t in texts) == 1


def test_same_basename_in_relative_folder_gets_distinct_outputs(tmp_path, monkeypatch, capsys):
    for sub in ("a", "b"):
        (tmp_path / "d" / sub).mkdir(parents=True)
        (tmp_path / "d" / sub / "notes.txt").write_text(f"from {sub}\n")
    (tmp_path / "d" / "a__notes.txt").write_text("flat twin\n")
    code, out = _rows(tmp_path, monkeypatch, capsys, Path("d"))
    assert code == 0
    outs = {r["cite"]: Path(r["out"]) for r in out["results"]}
    assert len(set(outs.values())) == 3
    assert "from a" in outs["d/a/notes.txt"].read_text()
    assert "from b" in outs["d/b/notes.txt"].read_text()
    assert "flat twin" in outs["d/a__notes.txt"].read_text()


def test_forwarded_mail_is_followed_and_depth_limit_is_reported(tmp_path, monkeypatch, capsys):
    mail = nested_mail(tmp_path / "fwd.eml", levels=1)
    _, out = _rows(tmp_path, monkeypatch, capsys, mail)
    kinds = sorted(r["kind"] for r in out["results"])
    assert kinds == ["mail", "mail", "text"]

    deep = nested_mail(tmp_path / "deep.eml", levels=3)
    _, out = _rows(tmp_path, monkeypatch, capsys, deep)
    assert len(out["results"]) == 3
    assert any("depth" in n for r in out["results"] for n in r["needs"])
