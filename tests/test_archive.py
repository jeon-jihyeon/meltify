import base64
import bz2
import contextvars
import gzip
import io
import json
import lzma
import shutil
import subprocess
import sys
import tarfile
import zipfile
from email.message import EmailMessage
from pathlib import Path

import pytest

from meltify import converters, passwords, tools
from meltify.cli import main
from meltify.converters import Entry, archive
from meltify.converters.run import RunContext, use
from meltify.evidence import Src
from tests.fixtures import make_archives as mk

SAMPLES = Path(__file__).parent / "fixtures" / "samples" / "archive"
# secret.txt holding "zip secret", written by 7-Zip with the password pw-1234
ZIPCRYPTO = (
    "UEsDBBQAAQAAANtrRl1QWKosFwAAAAsAAAAKAAAAc2VjcmV0LnR4dKbfczpTNIy1Jyjt+yelq2yZX3iBpsvxUEsBAj8D"
    "FAABAAAA22tGXVBYqiwXAAAACwAAAAoAJAAAAAAAAAAggKSBAAAAAHNlY3JldC50eHQKACAAAAAAAAEAGAByGKF4S1Xd"
    "AQAAAAAAAAAAAAAAAAAAAABQSwUGAAAAAAEAAQBcAAAAPwAAAAAA"
)
WINZIP_AES = (
    "UEsDBDMAAQBjANtrRl0AAAAAJwAAAAsAAAAKAAsAc2VjcmV0LnR4dAGZBwACAEFFAwAAYJsCSb64m8moS++ZVgz0UOXR"
    "SKAyFjpNTiocOMs68/BtceRUjQLFUEsBAj8DMwABAGMA22tGXQAAAAAnAAAACwAAAAoALwAAAAAAAAAggKSBAAAAAHNl"
    "Y3JldC50eHQKACAAAAAAAAEAGAByGKF4S1XdAQAAAAAAAAAAAAAAAAAAAAABmQcAAgBBRQMAAFBLBQYAAAAAAQABAGcA"
    "AABaAAAAAAA="
)


def _rows(tmp_path, monkeypatch, capsys, *paths):
    monkeypatch.chdir(tmp_path)
    code = main(["read", *map(str, paths), "--json", "--limit", "0"])
    out = json.loads(capsys.readouterr().out)
    return code, {r["cite"]: r for r in out["results"]}


def _md(row):
    return Path(row["out"]).read_text(encoding="utf-8")


def _libarchive():
    if not archive.libarchive_ready():
        pytest.skip("libarchive-c or the system libarchive isn't installed")


def test_nested_zip_cites_every_level(tmp_path, monkeypatch, capsys):
    outer = mk.nested_zip(tmp_path / "in")
    code, rows = _rows(tmp_path, monkeypatch, capsys, outer)
    assert code == 0

    top = rows[str(outer)]
    assert top["kind"] == "archive" and top["needs"] == []
    assert "| docs/b.txt | 13 |  |" in _md(top)

    assert f"## {outer}#att=docs/b.txt:1\n1| hello from b" in _md(rows[f"{outer}#att=docs/b.txt"])
    pdf = rows[f"{outer}#att=docs/report.pdf"]
    assert pdf["kind"] == "pdf"
    assert f"## {outer}#att=docs/report.pdf#p1\nZipped report says ship on Friday" in _md(pdf)

    inner = f"{outer}#att=mid.zip#att=inner.zip"
    assert f"## {inner}#att=c.txt:1\n1| inner text line" in _md(rows[f"{inner}#att=c.txt"])
    # The fourth level only shows up as a note on the archive that holds it
    assert rows[f"{inner}#att=deeper.zip"]["needs"] == ["1 nested items beyond depth 3"]
    assert not any("too-deep.txt" in c for c in rows)


def test_cp949_zip_names_are_restored(tmp_path, monkeypatch, capsys):
    path = mk.cp949_zip(tmp_path / "in")
    _, rows = _rows(tmp_path, monkeypatch, capsys, path)
    row = rows[f"{path}#att=한글/보고서.txt"]
    assert "1| 한국어 본문" in _md(row)


@pytest.mark.parametrize("suffix", sorted(mk.TAR_MODES))
def test_tar_variants(tmp_path, monkeypatch, capsys, suffix):
    path = mk.tar_variant(tmp_path / "in", suffix)
    _, rows = _rows(tmp_path, monkeypatch, capsys, path)
    assert rows[str(path)]["kind"] == "archive"
    member = rows[f"{path}#att=docs/b.txt"]
    assert f"## {path}#att=docs/b.txt:1\n1| tar line one\n2| tar line two" in _md(member)


def test_unsafe_zip_paths_are_neutralized_and_symlinks_skipped(tmp_path, monkeypatch, capsys):
    path = mk.evil_zip(tmp_path / "in")
    _, rows = _rows(tmp_path, monkeypatch, capsys, path)
    assert rows[str(path)]["needs"] == ["skipped link (symlink)"]
    listing = _md(rows[str(path)])
    assert "| ../../evil.txt | 3 | cited as evil.txt |" in listing
    assert "| link |  | skipped: symlink |" in listing
    for cite in ("evil.txt", "abs/evil.txt", "win/evil.txt", "ok.txt"):
        assert f"{path}#att={cite}" in rows
    out = tmp_path / "meltify-out"
    assert all(p.resolve().is_relative_to(out.resolve()) for p in out.rglob("*"))
    assert not (tmp_path / "evil.txt").exists() and not (tmp_path.parent / "evil.txt").exists()


def test_unsafe_tar_members(tmp_path, monkeypatch, capsys):
    path = mk.evil_tar(tmp_path / "in")
    _, rows = _rows(tmp_path, monkeypatch, capsys, path)
    assert rows[str(path)]["needs"] == [
        "skipped ln (symlink)",
        "skipped hard (hard link)",
        "skipped pipe (special file)",
    ]
    assert f"{path}#att=escape.txt" in rows
    assert "1| 본문" in _md(rows[f"{path}#att=한글.txt"])


def test_encrypted_member_is_skipped_and_the_rest_read(tmp_path, monkeypatch, capsys):
    path = mk.encrypted_zip(tmp_path / "in")
    _, rows = _rows(tmp_path, monkeypatch, capsys, path)
    assert rows[str(path)]["needs"] == [f"skipped secret.txt ({passwords.LOCKED})"]
    assert f"{path}#att=open.txt" in rows
    assert f"{path}#att=secret.txt" not in rows


def test_too_many_entries_rejects_the_whole_archive(tmp_path):
    path = mk.many_zip(tmp_path)
    out = archive.convert(path, Src(str(path)))
    assert out.children == [] and out.blocks == []
    assert out.needs == ["archive rejected: 20000 entries, over the 10000 limit"]


def test_ratio_bomb_zip_member_is_skipped(tmp_path):
    path = mk.ratio_zip(tmp_path)
    out = archive.convert(path, Src(str(path)))
    assert [c.name for c in out.children] == ["ok.txt"]
    assert out.needs == ["skipped zeros.bin (expands over 100:1)"]


def test_ratio_bomb_tar_stops_the_stream(tmp_path):
    path = mk.ratio_tgz(tmp_path)
    out = archive.convert(path, Src(str(path)))
    assert out.children == []
    assert out.needs == ["stopped at zeros.bin (expands over 100:1), later members unread"]


def test_entry_limit_checks_declared_and_read_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(archive, "MAX_MEMBER_BYTES", 10)
    path = mk.tar_variant(tmp_path, ".tar")
    out = archive.convert(path, Src(str(path)))
    assert out.needs == ["skipped docs/b.txt (over 10 bytes)"]

    # Formats like 7z may not declare a size, so the bytes actually read are counted too
    meter = archive.Meter("x", 1 << 30, streaming=False)
    liar = archive.Member("x.bin", size=None, chunks=lambda: [b"a" * 8, b"a" * 8])
    with pytest.raises(archive.Skip):
        archive._take(liar, meter)


def _run() -> contextvars.Context:
    """A context set up the way read sets one up for each run"""
    run = contextvars.copy_context()
    run.run(use, RunContext())
    return run


def test_total_budget_spans_nested_archives(tmp_path, monkeypatch):
    monkeypatch.setattr(archive, "MAX_TOTAL_BYTES", 40)
    top = mk.tar_variant(tmp_path, ".tar")
    run = _run()
    first = run.run(archive.convert, top, Src(str(top)))
    assert first.needs == []
    # A nested archive under the same input keeps charging the same budget
    nested = run.run(archive.convert, top, Src(str(top), parts=("again.tar",)))
    assert nested.children == []
    assert nested.needs == ["stopped at docs/b.txt (total over 40 bytes), later members unread"]
    # The next run starts over, even for an archive nested in something else
    assert _run().run(archive.convert, top, Src(str(top), parts=("again.tar",))).needs == []


def test_repeated_reads_of_a_zip_in_a_mail_dont_add_up(tmp_path, monkeypatch):
    import meltify

    member = tmp_path / "inner.zip"
    with zipfile.ZipFile(member, "w") as z:
        z.writestr("big.bin", b"x" * 300)
    mail = EmailMessage()
    mail["Subject"] = "zipped"
    mail.set_content("see attached")
    mail.add_attachment(member.read_bytes(), "application", "zip", filename="inner.zip")
    path = tmp_path / "z.eml"
    path.write_bytes(mail.as_bytes())
    # Room for one read, so a budget carried into the next would stop it
    monkeypatch.setattr(archive, "MAX_TOTAL_BYTES", 500)
    for n in range(3):
        env = meltify.read(path, out=tmp_path / f"out{n}", shallow=True)
        rows = {r["cite"]: r for r in env.results}
        assert rows[f"{path}#att=inner.zip"]["needs"] == []


def test_a_skipped_stream_member_still_charges_the_budget(monkeypatch):
    monkeypatch.setattr(archive, "MAX_TOTAL_BYTES", 100)
    meter = archive.Meter("x.7z", 1 << 20, streaming=True)
    # 7-Zip writes a symlink's bytes to the stream too, however many it lists
    members = iter(
        [
            archive.Member("link", 1 << 30, skip="symlink"),
            archive.Member("after.txt", 3, chunks=lambda: pytest.fail("read past the limit")),
        ]
    )
    out = archive._melt(
        members, Src("x.7z"), meter, archive.Listing(), converters.Converted("archive")
    )
    assert out.needs == ["skipped link (symlink)", "stopped after link (total or ratio limit)"]


def test_spilled_member_goes_to_a_temp_file(tmp_path, monkeypatch):
    monkeypatch.setattr(archive, "SPILL_BYTES", 4)
    path = mk.tar_variant(tmp_path, ".tar")
    out = archive.convert(path, Src(str(path)))
    child = out.children[0]
    assert child.data is None and child.path.read_bytes().startswith(b"tar line one")
    shutil.rmtree(child.path.parent)


def test_no_7z_or_rar_reader_reports_the_extra(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(archive, "libarchive_ready", lambda: False)
    monkeypatch.setattr(tools, "seven_zip", lambda: None)
    monkeypatch.setattr(archive.shutil, "which", lambda name: None)
    path = SAMPLES / "rar5-solid.rar"
    _, rows = _rows(tmp_path, monkeypatch, capsys, path)
    assert rows[str(path)]["needs"] == ["archive extra (meltify doctor --install archive)"]


def test_seven_zip_with_nested_zip(tmp_path, monkeypatch, capsys):
    _libarchive()
    path = mk.seven_zip(tmp_path / "in")
    _, rows = _rows(tmp_path, monkeypatch, capsys, path)
    assert "1| seven zip text" in _md(rows[f"{path}#att=docs/a.txt"])
    deep = rows[f"{path}#att=docs/inner.zip#att=c.txt"]
    assert f"## {path}#att=docs/inner.zip#att=c.txt:1\n1| inside 7z then zip" in _md(deep)


def test_rar_samples(tmp_path, monkeypatch, capsys):
    _libarchive()
    monkeypatch.setattr(tools, "seven_zip", lambda: None)
    sub = SAMPLES / "rar3-subdirs.rar"
    solid = SAMPLES / "rar5-solid.rar"
    evil = SAMPLES / "rar5-evil-symlink-traversal.rar"
    locked = SAMPLES / "rar5-hpsw.rar"
    _, rows = _rows(tmp_path, monkeypatch, capsys, sub, solid, evil, locked)
    assert f"{sub}#att=sub/dir1/file1.txt" in rows
    assert f"{sub}#att=sub/with space/long fn.txt" in rows
    assert f"{solid}#att=stest2.txt" in rows
    assert rows[str(evil)]["needs"] == ["skipped up (symlink)"]
    # libarchive can't decrypt RAR, so the need names 7-Zip
    assert rows[str(locked)]["needs"] == [archive.NEEDS_7ZIP]


def test_streamed_archive_over_the_entry_limit_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(archive, "MAX_ENTRIES", 2)
    path = mk.evil_tar(tmp_path)
    out = archive.convert(path, Src(str(path)))
    assert out.children == [] and out.needs == ["archive rejected: over 2 entries"]


COMPRESS = {".gz": gzip.compress, ".bz2": bz2.compress, ".xz": lzma.compress}


def _single(monkeypatch):
    # Registered here until the registry lists the single-file suffixes itself
    entry = Entry("archive", "meltify.converters.archive:convert")
    for suffix in COMPRESS:
        monkeypatch.setitem(converters.SUFFIXES, suffix, entry)


@pytest.mark.parametrize("suffix", sorted(COMPRESS))
def test_single_file_compression_melts_the_inner_file(tmp_path, monkeypatch, capsys, suffix):
    _single(monkeypatch)
    path = tmp_path / "in" / f"notes.txt{suffix}"
    path.parent.mkdir()
    path.write_bytes(COMPRESS[suffix](b"first line\nsecond line\n"))
    code, rows = _rows(tmp_path, monkeypatch, capsys, path)
    assert code == 0
    assert rows[str(path)]["kind"] == "archive" and rows[str(path)]["needs"] == []
    assert "| notes.txt | 23 |  |" in _md(rows[str(path)])
    inner = rows[f"{path}#att=notes.txt"]
    assert f"## {path}#att=notes.txt:1\n1| first line\n2| second line" in _md(inner)


def test_compressed_file_inside_a_zip_keeps_its_member_name(tmp_path, monkeypatch, capsys):
    _single(monkeypatch)
    path = tmp_path / "bundle.zip"
    path.write_bytes(mk._zip({"logs/한글.log.gz": gzip.compress(b"inner log line\n")}))
    _, rows = _rows(tmp_path, monkeypatch, capsys, path)
    row = rows[f"{path}#att=logs/한글.log.gz#att=한글.log"]
    assert "1| inner log line" in _md(row)


def test_tarball_named_plain_gz_is_read_as_a_tar(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        info = tarfile.TarInfo("docs/a.txt")
        info.size = 4
        t.addfile(info, io.BytesIO(b"tar\n"))
    path = tmp_path / "backup.gz"
    path.write_bytes(gzip.compress(buf.getvalue()))
    out = archive.convert(path, Src(str(path)))
    assert [c.name for c in out.children] == ["docs/a.txt"]


def test_single_file_bomb_stops_at_the_ratio(tmp_path):
    path = tmp_path / "zeros.bin.xz"
    path.write_bytes(lzma.compress(bytes(8 << 20)))
    out = archive.convert(path, Src(str(path)))
    assert out.children == []
    assert out.needs == ["stopped at zeros.bin (expands over 100:1), later members unread"]


def test_corrupt_single_file_is_listed_as_unreadable(tmp_path):
    path = tmp_path / "broken.txt.gz"
    path.write_bytes(gzip.compress(b"some text\n")[:-12])
    out = archive.convert(path, Src(str(path)))
    assert out.children == []
    assert len(out.needs) == 1 and out.needs[0].startswith("skipped broken.txt (unreadable, ")


def _fixture(tmp_path: Path, name: str, encoded: str) -> Path:
    path = tmp_path / name
    path.write_bytes(base64.b64decode(encoded))
    return path


def _children(out) -> dict[str, bytes]:
    return {c.name: c.data if c.path is None else c.path.read_bytes() for c in out.children}


def test_zipcrypto_member_opens_with_the_password(tmp_path, monkeypatch):
    path = _fixture(tmp_path, "locked.zip", ZIPCRYPTO)
    out = archive.convert(path, Src(str(path)))
    assert out.needs == [f"skipped secret.txt ({passwords.LOCKED})"]

    monkeypatch.setenv("MELTIFY_PASSWORD", "not it")
    out = archive.convert(path, Src(str(path)))
    assert out.children == [] and out.needs == [f"skipped secret.txt ({passwords.WRONG})"]

    monkeypatch.setenv("MELTIFY_PASSWORD", "pw-1234")
    out = archive.convert(path, Src(str(path)))
    assert out.needs == [] and _children(out) == {"secret.txt": b"zip secret\n"}


def test_aes_zip_without_pyzipper_or_7zip_names_the_extra(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "pyzipper", None)
    monkeypatch.setattr(tools, "seven_zip", lambda: None)
    monkeypatch.setenv("MELTIFY_PASSWORD", "pw-1234")
    path = _fixture(tmp_path, "aes.zip", WINZIP_AES)
    out = archive.convert(path, Src(str(path)))
    assert out.needs == [f"skipped secret.txt ({passwords.NEEDS_CRYPTO})"]


def test_aes_zip_with_pyzipper(tmp_path, monkeypatch):
    pytest.importorskip("pyzipper")
    path = _fixture(tmp_path, "aes.zip", WINZIP_AES)
    monkeypatch.setenv("MELTIFY_PASSWORD", "wrong")
    out = archive.convert(path, Src(str(path)))
    assert out.needs == [f"skipped secret.txt ({passwords.WRONG})"]
    monkeypatch.setenv("MELTIFY_PASSWORD", "pw-1234")
    assert _children(archive.convert(path, Src(str(path)))) == {"secret.txt": b"zip secret\n"}


def _7zz() -> str:
    found = tools.seven_zip()
    if found is None:
        pytest.skip("7-Zip isn't installed")
    # The p7zip fork stores a symlink as a plain file, so these fixtures need 7-Zip itself
    if b"p7zip" in subprocess.run([found], capture_output=True).stdout:
        pytest.skip("p7zip instead of 7-Zip")
    return found


def test_a_wrong_password_from_7zip_without_one_set_is_only_locked(monkeypatch):
    monkeypatch.delenv("MELTIFY_PASSWORD", raising=False)
    output = b"ERROR: Data Error in encrypted file. Wrong password? : a.txt"
    assert str(archive._failure("7-Zip", output)) == passwords.LOCKED


def _seven(tmp_path: Path, name: str, *flags: str) -> Path:
    """A 7z built by 7-Zip itself, with a folder, an empty file and a symlink"""
    src = tmp_path / "src"
    if not src.exists():
        (src / "sub").mkdir(parents=True)
        (src / "a.txt").write_bytes(b"hello one\n")
        (src / "sub" / "b.txt").write_bytes(b"second file here\n")
        (src / "empty.txt").write_bytes(b"")
        (src / "link").symlink_to("/etc/passwd")
    path = tmp_path / name
    # A password in argv is fine for a throwaway fixture, never for meltify itself
    cmd = [_7zz(), "a", "-bd", "-snl", *flags, str(path), "a.txt", "sub", "empty.txt", "link"]
    subprocess.run(cmd, cwd=src, check=True, capture_output=True)
    return path


def test_7zip_reads_7z_by_listed_sizes(tmp_path, monkeypatch):
    path = _seven(tmp_path, "plain.7z")
    monkeypatch.setattr(archive, "libarchive_ready", lambda: False)
    out = archive.convert(path, Src(str(path)))
    assert out.needs == ["skipped link (symlink)"]
    assert _children(out) == {
        "a.txt": b"hello one\n",
        "sub/b.txt": b"second file here\n",
        "empty.txt": b"",
    }


@pytest.mark.parametrize("flags", [["-pSecret1"], ["-pSecret1", "-mhe=on"]])
def test_7zip_decrypts_with_the_password_from_stdin(tmp_path, monkeypatch, flags):
    path = _seven(tmp_path, "locked.7z", *flags)
    argv: list[list[str]] = []
    real_run, real_popen = subprocess.run, subprocess.Popen

    def run(cmd, *a, **k):
        argv.append(cmd)
        return real_run(cmd, *a, **k)

    def popen(cmd, *a, **k):
        argv.append(cmd)
        return real_popen(cmd, *a, **k)

    monkeypatch.setattr(archive.subprocess, "run", run)
    monkeypatch.setattr(archive.subprocess, "Popen", popen)
    assert archive.convert(path, Src(str(path))).needs == [passwords.LOCKED]
    monkeypatch.setenv("MELTIFY_PASSWORD", "not it")
    assert archive.convert(path, Src(str(path))).needs == [passwords.WRONG]
    monkeypatch.setenv("MELTIFY_PASSWORD", "Secret1")
    out = archive.convert(path, Src(str(path)))
    assert out.needs == ["skipped link (symlink)"] and _children(out)["a.txt"] == b"hello one\n"
    assert argv and not any("Secret1" in arg for cmd in argv for arg in cmd)


def test_7zip_stream_skips_an_oversized_member_and_reads_on(tmp_path, monkeypatch):
    path = _seven(tmp_path, "plain.7z")
    monkeypatch.setattr(archive, "libarchive_ready", lambda: False)
    monkeypatch.setattr(archive, "MAX_MEMBER_BYTES", 12)
    out = archive.convert(path, Src(str(path)))
    assert "skipped sub/b.txt (over 12 bytes)" in out.needs
    assert set(_children(out)) == {"a.txt", "empty.txt"}


def test_7zip_rejects_too_many_entries(tmp_path, monkeypatch):
    path = _seven(tmp_path, "plain.7z")
    monkeypatch.setattr(archive, "libarchive_ready", lambda: False)
    monkeypatch.setattr(archive, "MAX_ENTRIES", 2)
    out = archive.convert(path, Src(str(path)))
    assert out.needs == ["archive rejected: 4 entries, over the 2 limit"]


def test_7zip_reads_rar_samples_and_their_password(tmp_path, monkeypatch, capsys):
    _7zz()
    monkeypatch.setattr(archive, "libarchive_ready", lambda: False)
    evil = SAMPLES / "rar5-evil-symlink-traversal.rar"
    locked = SAMPLES / "rar5-hpsw.rar"
    _, rows = _rows(tmp_path, monkeypatch, capsys, evil, locked)
    assert rows[str(evil)]["needs"] == ["skipped up (symlink)"]
    assert rows[str(locked)]["needs"] == [passwords.LOCKED]
    out = tmp_path / "meltify-out"
    assert all(p.resolve().is_relative_to(out.resolve()) for p in out.rglob("*"))

    monkeypatch.setenv("MELTIFY_PASSWORD", "password")
    _, rows = _rows(tmp_path, monkeypatch, capsys, locked)
    assert rows[str(locked)]["needs"] == []
    assert "1| 000" in _md(rows[f"{locked}#att=stest1.txt"])


def test_7zip_opens_aes_zip_when_pyzipper_is_missing(tmp_path, monkeypatch):
    _7zz()
    monkeypatch.setitem(sys.modules, "pyzipper", None)
    monkeypatch.setattr(archive, "find_spec", lambda name: None)
    monkeypatch.setenv("MELTIFY_PASSWORD", "pw-1234")
    path = _fixture(tmp_path, "aes.zip", WINZIP_AES)
    assert _children(archive.convert(path, Src(str(path)))) == {"secret.txt": b"zip secret\n"}


def _bsdtar(monkeypatch):
    if shutil.which("bsdtar") is None:
        pytest.skip("bsdtar isn't installed")
    monkeypatch.setattr(archive, "libarchive_ready", lambda: False)
    monkeypatch.setattr(tools, "seven_zip", lambda: None)


def test_bsdtar_reads_rar_as_a_tar_stream(tmp_path, monkeypatch, capsys):
    _bsdtar(monkeypatch)
    sub = SAMPLES / "rar3-subdirs.rar"
    evil = SAMPLES / "rar5-evil-symlink-traversal.rar"
    locked = SAMPLES / "rar5-hpsw.rar"
    _, rows = _rows(tmp_path, monkeypatch, capsys, sub, evil, locked)
    assert "1| file1" in _md(rows[f"{sub}#att=sub/dir1/file1.txt"])
    assert rows[str(evil)]["needs"] == ["skipped up (symlink)"]
    assert rows[str(locked)]["needs"] == [archive.NEEDS_7ZIP]


def test_bsdtar_failure_reads_stderr_that_isnt_utf8():
    # A Latin-1 file name in bsdtar's message must not turn into a UnicodeDecodeError
    stderr = b"bsdtar: caf\xe9.txt: Damaged archive\nbsdtar: Error exit delayed\n"
    e = archive._bsdtar_failure(stderr)
    assert isinstance(e, OSError) and str(e) == "bsdtar: caf�.txt: Damaged archive"
