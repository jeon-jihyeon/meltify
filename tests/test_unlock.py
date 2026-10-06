import json
import sys
import tempfile
from pathlib import Path

import pytest

from meltify import passwords
from meltify.cli import main
from meltify.converters import unlock
from meltify.engines import ocr as engines
from tests.fixtures.make_binary import locked_xlsx

SECRET = "correct horse 7"
SAMPLES = Path(__file__).parent / "fixtures" / "samples"


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    monkeypatch.delenv("MELTIFY_PASSWORD", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(engines, "select", lambda spec, s: [])
    # Decrypted copies land here, so a test can check they're gone
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))


def _read(tmp_path, monkeypatch, capsys, *args):
    monkeypatch.chdir(tmp_path)
    code = main(["read", *map(str, args), "--json", "--limit", "0"])
    raw = capsys.readouterr().out
    return code, raw, json.loads(raw)


def _locked_pdf(path: Path) -> Path:
    import pymupdf

    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Locked memo says ship on Tuesday", fontsize=12)
    doc.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw=SECRET, owner_pw="owner")
    return path


def _locked_xlsx(path: Path) -> Path:
    return locked_xlsx(path, SECRET)


def _leftovers(tmp_path: Path) -> list[Path]:
    return list((tmp_path / "tmp").glob("meltify-unlock-*"))


def test_encrypted_pdf_without_a_password_is_a_need(tmp_path, monkeypatch, capsys):
    pdf = _locked_pdf(tmp_path / "memo.pdf")
    code, _, out = _read(tmp_path, monkeypatch, capsys, pdf)
    assert code == 0
    [row] = out["results"]
    assert row["kind"] == "pdf" and row["needs"] == [passwords.LOCKED]


def test_wrong_password_is_a_need(tmp_path, monkeypatch, capsys):
    pdf = _locked_pdf(tmp_path / "memo.pdf")
    monkeypatch.setenv("MELTIFY_PASSWORD", "nope")
    _, _, out = _read(tmp_path, monkeypatch, capsys, pdf)
    assert out["results"][0]["needs"] == [passwords.WRONG]


def test_password_file_decrypts_and_cites_the_original(tmp_path, monkeypatch, capsys):
    pdf = _locked_pdf(tmp_path / "memo.pdf")
    secret = tmp_path / "pw.txt"
    secret.write_text(f"{SECRET}\nignored second line\n")
    _, raw, out = _read(tmp_path, monkeypatch, capsys, pdf, "--password-file", secret)
    [row] = out["results"]
    assert row["cite"] == str(pdf) and row["needs"] == []
    md = Path(row["out"]).read_text()
    assert f"## {pdf}#p1\nLocked memo says ship on Tuesday" in md
    assert SECRET not in raw and SECRET not in md
    assert _leftovers(tmp_path) == []


def test_password_flag_works_but_warns(tmp_path, monkeypatch, capsys):
    pdf = _locked_pdf(tmp_path / "memo.pdf")
    _, raw, out = _read(tmp_path, monkeypatch, capsys, pdf, "--password", SECRET)
    assert out["results"][0]["needs"] == []
    assert any("--password is visible to other users" in w for w in out["warnings"])
    assert SECRET not in raw


def test_password_file_beats_the_env_and_the_flag(tmp_path, monkeypatch, capsys):
    pdf = _locked_pdf(tmp_path / "memo.pdf")
    secret = tmp_path / "pw.txt"
    secret.write_text(SECRET, encoding="utf-8-sig")
    monkeypatch.setenv("MELTIFY_PASSWORD", "env is wrong")
    args = (pdf, "--password-file", secret, "--password", "flag is wrong")
    _, _, out = _read(tmp_path, monkeypatch, capsys, *args)
    assert out["results"][0]["needs"] == []


def test_password_flag_beats_the_env(tmp_path, monkeypatch, capsys):
    pdf = _locked_pdf(tmp_path / "memo.pdf")
    monkeypatch.setenv("MELTIFY_PASSWORD", "env is wrong")
    _, _, out = _read(tmp_path, monkeypatch, capsys, pdf, "--password", SECRET)
    assert out["results"][0]["needs"] == []
    assert any("--password is visible to other users" in w for w in out["warnings"])


def test_empty_password_file_is_a_usage_error(tmp_path, monkeypatch, capsys):
    pdf = _locked_pdf(tmp_path / "memo.pdf")
    (tmp_path / "pw.txt").write_text("\n")
    code, _, out = _read(tmp_path, monkeypatch, capsys, pdf, "--password-file", "pw.txt")
    assert code == 2 and "no password on the first line" in out["errors"][0]["message"]


def test_encrypted_xlsx_is_decrypted_into_a_temp_copy(tmp_path, monkeypatch, capsys):
    book = _locked_xlsx(tmp_path / "plan.xlsx")
    _, _, out = _read(tmp_path, monkeypatch, capsys, book)
    assert out["results"][0]["needs"] == [passwords.LOCKED]

    monkeypatch.setenv("MELTIFY_PASSWORD", "nope")
    _, _, out = _read(tmp_path, monkeypatch, capsys, book)
    assert out["results"][0]["needs"] == [passwords.WRONG]

    monkeypatch.setenv("MELTIFY_PASSWORD", SECRET)
    _, _, out = _read(tmp_path, monkeypatch, capsys, book)
    [row] = out["results"]
    assert row["kind"] == "sheet" and row["needs"] == ["hidden sheets Old"]
    assert "| 7 |  | budget review |" in Path(row["out"]).read_text()
    assert _leftovers(tmp_path) == []


def test_encrypted_package_without_msoffcrypto_names_the_extra(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "msoffcrypto", None)
    # Any OLE container under a .docx name is an encrypted package
    path = tmp_path / "locked.docx"
    path.write_bytes(unlock.OLE + bytes(504))
    with pytest.raises(passwords.Locked) as e:
        unlock.unlock(path)
    assert str(e.value) == unlock.NEEDS_CRYPTO


@pytest.mark.parametrize(
    "path",
    [
        SAMPLES / "legacy" / "SampleDoc.doc",
        SAMPLES / "legacy" / "SampleSS.xls",
        SAMPLES / "docs" / "hwplib-table.hwp",
    ],
)
def test_plain_ole_files_pass_through(path):
    pytest.importorskip("msoffcrypto")
    assert unlock.unlock(path) == path


def test_plain_pdf_passes_through_without_a_locked_open(tmp_path, monkeypatch):
    import pymupdf

    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "nothing secret", fontsize=12)
    plain = tmp_path / "plain.pdf"
    doc.save(plain, use_objstms=1)
    opened = []
    real = pymupdf.open
    monkeypatch.setattr(pymupdf, "open", lambda *a, **k: opened.append(a) or real(*a, **k))
    assert unlock.unlock(plain) == plain and opened == []


def test_encrypted_pdf_with_an_xref_stream_is_still_caught(tmp_path, monkeypatch):
    import pymupdf

    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "streamed xref", fontsize=12)
    path = tmp_path / "locked.pdf"
    doc.save(path, use_objstms=1, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw=SECRET)
    with pytest.raises(passwords.Locked):
        unlock.unlock(path)


def test_plain_pdf_and_other_files_pass_through(tmp_path):
    import pymupdf

    doc = pymupdf.open()
    doc.new_page()
    pdf = tmp_path / "plain.pdf"
    doc.save(pdf)
    text = tmp_path / "a.txt"
    text.write_text("x")
    assert unlock.unlock(pdf) == pdf and unlock.unlock(text) == text


def _raw_pdf(path: Path, body: bytes) -> Path:
    """A plain PDF whose one content stream holds `body` as is"""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(body) + body + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    path.write_bytes(bytes(out))
    return path


def test_encrypt_named_in_page_content_doesnt_open_a_big_plain_pdf(tmp_path, monkeypatch):
    import pymupdf

    # Only trailers can mark encryption, so the middle of a big file is never read
    pad = b"% " + b"x" * 1022 + b"\n"
    body = pad * 2048 + b"% a page that talks about /Encrypt\n" + pad * 2048
    plain = _raw_pdf(tmp_path / "plain.pdf", body)
    with pymupdf.open(plain) as doc:
        assert doc.page_count == 1 and not doc.needs_pass
    opened = []
    monkeypatch.setattr(pymupdf, "open", lambda *a, **k: opened.append(a))
    assert unlock.unlock(plain) == plain and opened == []


@pytest.mark.parametrize("objstms", [0, 1])
def test_a_big_encrypted_pdf_is_caught_from_its_trailer(tmp_path, objstms):
    import os

    import pymupdf

    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "big and locked", fontsize=12)
    doc.embfile_add("blob.bin", os.urandom(3 << 20))
    path = tmp_path / "locked.pdf"
    doc.save(path, use_objstms=objstms, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw=SECRET)
    assert path.stat().st_size > unlock.HEAD_WINDOW + unlock.TRAILER_WINDOW
    with pytest.raises(passwords.Locked):
        unlock.unlock(path)
