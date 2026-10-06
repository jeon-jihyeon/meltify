import json
import zipfile
from pathlib import Path

import pytest

from meltify.cli import main
from meltify.engines import asr
from meltify.engines import ocr as engines
from meltify.safe import MissingTool
from tests.fixtures.make_docs import card, embedded_docx, mixed_folder, nested_mail, tricky_mail
from tests.fixtures.make_pdf import hidden_pdf


@pytest.fixture(autouse=True)
def no_engines(tmp_path, monkeypatch):
    # Real engines are slow and machine-specific, so these tests see none unless they add fakes
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(engines, "select", lambda spec, s: [])

    def no_asr(spec, s):
        raise MissingTool("speech engine", "install one")

    monkeypatch.setattr(asr, "select", no_asr)


def _rows(tmp_path, monkeypatch, capsys, *paths):
    monkeypatch.chdir(tmp_path)
    code = main(["read", *map(str, paths), "--json", "--limit", "0"])
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("shallow", [True, False])
def test_mixed_folder_melts_with_citations(tmp_path, monkeypatch, capsys, shallow):
    # --shallow is the 0.1 behavior, and without engines the deep read lists the same needs
    folder = mixed_folder(tmp_path / "in")
    flags = ["--shallow"] if shallow else []
    code, out = _rows(tmp_path, monkeypatch, capsys, folder, *flags)
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

    deep = nested_mail(tmp_path / "deep.eml", levels=4)
    _, out = _rows(tmp_path, monkeypatch, capsys, deep)
    assert len(out["results"]) == 4
    assert [r["needs"] for r in out["results"] if r["needs"]] == [["1 nested items beyond depth 3"]]
    assert max(r["cite"].count("#att=") for r in out["results"]) == 3


def test_unknown_binary_is_rendered_and_cited_by_page(tmp_path, monkeypatch, capsys):
    import tempfile

    import pymupdf

    from meltify.converters import render

    def to_pdf(path, timeout=render.TIMEOUT, out_dir=None):
        doc = pymupdf.open()
        doc.new_page().insert_text((72, 72), "Works memo says ship on Monday")
        doc.new_page()
        doc.save(out_dir / f"{path.stem}.pdf")
        return out_dir / f"{path.stem}.pdf"

    # The rendered PDF outlives the call while page 2 waits for OCR
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(render, "to_pdf", to_pdf)
    # Microsoft Works, which only LibreOffice reads
    memo = tmp_path / "memo.wps"
    memo.write_bytes(b"\x00\x01binary")
    code, out = _rows(tmp_path, monkeypatch, capsys, memo)
    assert code == 0
    [row] = out["results"]
    assert row["kind"] == "rendered" and row["needs"] == ["ocr pages 2"]
    assert f"## {memo}#p1\nWorks memo says ship on Monday" in Path(row["out"]).read_text()


def test_a_read_run_leaves_no_temp_dirs_behind(tmp_path, monkeypatch, capsys):
    """Renders, decrypted copies and spilled members last through OCR, then go with the run"""
    import importlib.util
    import tempfile

    import pymupdf

    from meltify.commands import read
    from meltify.converters import archive, doc, ppt, render
    from meltify.engines.ocr import LOCAL, TextBox
    from tests.fixtures.make_archives import tar_variant
    from tests.fixtures.make_binary import compound_file, locked_xlsx

    secret = "correct horse 7"

    def two_pages(target: Path) -> Path:
        doc = pymupdf.open()
        doc.new_page().insert_text((72, 72), "Page one has text")
        doc.new_page()  # image-only, so OCR opens the PDF after every file is converted
        doc.save(target, encryption=pymupdf.PDF_ENCRYPT_NONE)
        return target

    class Reader:
        name, kind = "fake", LOCAL

        def missing(self):
            return None

        def recognize(self, image, size):
            return [TextBox("SCANNED", (0, 0, size[0] / 2, size[1] / 4), 0.9)]

    temp = tmp_path / "tmp"
    temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    monkeypatch.setattr(engines, "select", lambda spec, s: [Reader()])
    # LibreOffice for the fallback render and a PowerPoint 95 deck
    monkeypatch.setattr(render, "to_pdf", lambda p, timeout=0, out_dir=None: two_pages(
        out_dir / f"{p.stem}.pdf"))  # fmt: skip
    monkeypatch.setattr(doc, "soffice", lambda: "/opt/soffice")
    monkeypatch.setattr(ppt, "soffice", lambda: "/opt/soffice")
    monkeypatch.setattr(doc, "convert_to", lambda p, target, out_dir, timeout: two_pages(
        out_dir / f"{p.stem}.pdf"))  # fmt: skip
    # Spill every member to disk, and stop nesting before the spilled one is moved out
    monkeypatch.setattr(archive, "SPILL_BYTES", 4)
    monkeypatch.setattr(read, "MAX_DEPTH", 0)
    monkeypatch.setenv("MELTIFY_PASSWORD", secret)

    inputs = tmp_path / "in"
    inputs.mkdir()
    (inputs / "memo.wps").write_bytes(b"\x00\x01binary")
    (inputs / "old.ppt").write_bytes(compound_file({"PP40": b"\0" * 64}))
    locked = pymupdf.open()
    locked.new_page().insert_text((72, 72), "Locked page one")
    locked.new_page()
    locked.save(inputs / "locked.pdf", encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw=secret)
    tar_variant(inputs, ".tar")
    if importlib.util.find_spec("msoffcrypto"):
        locked_xlsx(inputs / "locked.xlsx", secret)
    code, out = _rows(tmp_path, monkeypatch, capsys, inputs)
    assert code == 0
    rows = {Path(r["cite"].split("#")[0]).name: r for r in out["results"]}
    # Each rendered or decrypted PDF was still there when OCR read its blank page
    for name in ("memo.wps", "old.ppt", "locked.pdf"):
        assert rows[name]["needs"] == [] and "SCANNED" in Path(rows[name]["out"]).read_text()
    assert "beyond depth 0" in " ".join(rows["bundle.tar"]["needs"])
    assert list(temp.iterdir()) == []


def test_names_that_differ_only_in_case_get_their_own_outputs(tmp_path, monkeypatch, capsys):
    # macOS and Windows see Readme.txt and README.txt as one file
    path = tmp_path / "case.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Readme.txt", "lower readme\n")
        z.writestr("README.txt", "upper readme\n")
    code, out = _rows(tmp_path, monkeypatch, capsys, path)
    assert code == 0
    outs = {r["cite"]: r["out"] for r in out["results"]}
    assert len({o.casefold() for o in outs.values()}) == 3
    assert "lower readme" in Path(outs[f"{path}#att=Readme.txt"]).read_text()
    assert "upper readme" in Path(outs[f"{path}#att=README.txt"]).read_text()
    saved = list((tmp_path / "meltify-out" / "read" / "attachments" / "case.zip").iterdir())
    assert len({p.name.casefold() for p in saved}) == 2


class Panic(BaseException):
    """What pyo3 raises for a Rust panic, like python-calamine's on a truncated .xls"""


def test_a_parser_panic_fails_only_its_own_file(tmp_path, monkeypatch, capsys):
    from meltify import converters

    folder = tmp_path / "in"
    folder.mkdir()
    (folder / "book.xlsx").write_bytes(b"PK\x03\x04 cut short")
    (folder / "notes.txt").write_text("still melted\n")
    real = converters.pick

    def panic(path, src):
        raise Panic("range start index 116 out of range for slice of length 81")

    monkeypatch.setattr(
        converters, "pick", lambda p: ("sheet", panic) if p.suffix == ".xlsx" else real(p)
    )
    code, out = _rows(tmp_path, monkeypatch, capsys, folder)
    rows = {Path(r["cite"]).name: r for r in out["results"]}
    assert rows["book.xlsx"]["error"].startswith("Panic: range start index 116")
    assert "still melted" in Path(rows["notes.txt"]["out"]).read_text()


def test_shallow_lists_embedded_pictures_like_a_read_without_engines(tmp_path, monkeypatch, capsys):
    docx = embedded_docx(tmp_path / "pics.docx", card("CHART 42", (600, 200)))
    needs = [
        _rows(tmp_path, monkeypatch, capsys, docx, *f)[1]["results"][0]["needs"]
        for f in ([], ["--shallow"])
    ]
    assert needs[0][0] == "ocr"
    # A deep read tries to draw the EMF, while --shallow starts no renderer for it
    shallow = [
        n.replace("not drawn under --shallow", "no renderer could draw it") for n in needs[1]
    ]
    assert shallow == needs[0]


def test_a_bundle_folder_given_alone_keeps_its_own_name(tmp_path):
    from meltify import files

    bundle = tmp_path / "in/old.pages"
    (bundle / "Data").mkdir(parents=True)
    (bundle / "index.xml.gz").write_text("1")
    found = list(files.iter_files([bundle]))
    assert files.common_root([bundle]) == tmp_path / "in"
    assert files.flat_names(found, files.common_root([bundle])) == {bundle: "old.pages"}


def _outputs(rows):
    root = Path(rows[0]["out"]).parent
    return {Path(r["out"]).name: Path(r["out"]).read_text().replace(str(root), "OUT") for r in rows}


def test_archive_members_melt_side_by_side_with_stable_names(tmp_path, monkeypatch, capsys):
    import threading
    import time

    from meltify.converters import text

    real = text.convert
    state = {"running": 0, "peak": 0}
    lock = threading.Lock()

    def slow(path, src):
        with lock:
            state["running"] += 1
            state["peak"] = max(state["peak"], state["running"])
        time.sleep(0.05)
        with lock:
            state["running"] -= 1
        return real(path, src)

    monkeypatch.setattr(text, "convert", slow)
    inner = tmp_path / "inner.zip"
    with zipfile.ZipFile(inner, "w") as z:
        z.writestr("deep.txt", "deep line\n")
    path = tmp_path / "many.zip"
    with zipfile.ZipFile(path, "w") as z:
        for i in range(12):
            z.writestr(f"d{i % 3}/f{i}.txt", f"member {i}\n")
        z.writestr("d0/f0.txt.md", "a name that flattens near another\n")
        z.write(inner, "nested/inner.zip")
    runs = {}
    for jobs in ("4", "1"):
        state["peak"] = 0
        monkeypatch.chdir(tmp_path)
        code = main(["read", str(path), "--json", "--limit", "0", "--jobs", jobs,
                     "--out", str(tmp_path / f"out{jobs}")])  # fmt: skip
        assert code == 0
        rows = json.loads(capsys.readouterr().out)["results"]
        runs[jobs] = (_outputs(rows), [r["cite"] for r in rows], state["peak"])
    assert runs["4"][2] > 1 and runs["1"][2] == 1
    assert runs["4"][:2] == runs["1"][:2]
    assert f"{path}#att=nested/inner.zip#att=deep.txt" in runs["4"][1]


def test_pdfs_convert_in_worker_processes_with_the_same_output(tmp_path, monkeypatch, capsys):
    from meltify.commands import read
    from tests.fixtures.make_docs import embedded_pdf

    folder = tmp_path / "in"
    folder.mkdir()
    embedded_pdf(folder / "report.pdf")
    hidden_pdf(folder / "hidden.pdf")
    (folder / "broken.pdf").write_bytes(b"%PDF-1.4 cut short")
    used = []
    real_close = read.PdfPool.close

    def close(self):
        used.append(self.executor is not None and not self.broken)
        real_close(self)

    monkeypatch.setattr(read, "_spawn_safe", lambda: True)
    monkeypatch.setattr(read.PdfPool, "close", close)
    monkeypatch.chdir(tmp_path)
    runs = {}
    for jobs in ("2", "1"):
        code = main(["read", str(folder), "--json", "--limit", "0", "--jobs", jobs,
                     "--out", str(tmp_path / f"out{jobs}")])  # fmt: skip
        assert code == 0
        rows = json.loads(capsys.readouterr().out)["results"]
        runs[jobs] = (_outputs([r for r in rows if r.get("out")]), rows)
    # Only the run with more than one job starts the pool, and its processes read every PDF
    assert used == [True]
    assert runs["2"][0] == runs["1"][0]
    errors = {Path(r["cite"]).name: r.get("error") for r in runs["2"][1]}
    assert (
        errors["broken.pdf"]
        and errors["broken.pdf"]
        == {Path(r["cite"]).name: r.get("error") for r in runs["1"][1]}["broken.pdf"]
    )


def test_pdf_pool_hands_back_what_it_cant_take(tmp_path, monkeypatch):
    from concurrent.futures.process import BrokenProcessPool

    from meltify.commands import read
    from meltify.converters import pdf
    from meltify.evidence import Src

    pool = read.PdfPool(1)
    # A swapped in converter may not pickle, so it stays in this process
    assert pool.convert(lambda p, s: None, tmp_path / "a.pdf", Src("a.pdf")) is None
    assert pool.executor is None

    class Broken:
        def submit(self, *a, **k):
            raise BrokenProcessPool("a worker died")

    monkeypatch.setattr(pool, "_executor", lambda: Broken())
    assert pool.convert(pdf.convert, tmp_path / "a.pdf", Src("a.pdf")) is None
    assert pool.broken
