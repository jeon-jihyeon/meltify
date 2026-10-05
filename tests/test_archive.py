import json
from pathlib import Path

import pytest

from meltify.cli import main
from meltify.converters import archive
from meltify.evidence import Src
from tests.fixtures import make_archives as mk

SAMPLES = Path(__file__).parent / "fixtures" / "samples" / "archive"


def _rows(tmp_path, monkeypatch, capsys, *paths):
    monkeypatch.chdir(tmp_path)
    code = main(["read", *map(str, paths), "--json", "--limit", "0"])
    out = json.loads(capsys.readouterr().out)
    return code, {r["cite"]: r for r in out["results"]}


def _md(row):
    return Path(row["out"]).read_text(encoding="utf-8")


def _libarchive():
    if not archive._libarchive_ready():
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
    assert rows[str(path)]["needs"] == ["skipped secret.txt (encrypted)"]
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
    monkeypatch.setattr(archive, "MAX_ENTRY_BYTES", 10)
    path = mk.tar_variant(tmp_path, ".tar")
    out = archive.convert(path, Src(str(path)))
    assert out.needs == ["skipped docs/b.txt (over 10 bytes)"]

    # Formats like 7z may not declare a size, so the bytes actually read are counted too
    meter = archive.Meter("x", 1 << 30, streaming=False)
    liar = archive.Member("x.bin", size=None, chunks=lambda: [b"a" * 8, b"a" * 8])
    with pytest.raises(archive.Skip):
        archive._take(liar, meter)


def test_total_budget_spans_nested_archives(tmp_path, monkeypatch):
    monkeypatch.setattr(archive, "MAX_TOTAL_BYTES", 40)
    top = mk.tar_variant(tmp_path, ".tar")
    first = archive.convert(top, Src(str(top)))
    assert first.needs == []
    # A nested archive under the same input keeps charging the same budget
    nested = archive.convert(top, Src(str(top), parts=("again.tar",)))
    assert nested.children == []
    assert nested.needs == ["stopped at docs/b.txt (total over 40 bytes), later members unread"]
    # A new top-level read starts over
    assert archive.convert(top, Src(str(top))).needs == []


def test_spilled_member_goes_to_a_temp_file(tmp_path, monkeypatch):
    monkeypatch.setattr(archive, "SPILL_BYTES", 4)
    path = mk.tar_variant(tmp_path, ".tar")
    out = archive.convert(path, Src(str(path)))
    child = out.children[0]
    assert child.data is None and child.path.read_bytes().startswith(b"tar line one")
    child.path.unlink()


def test_missing_libarchive_reports_the_extra(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(archive, "_libarchive_ready", lambda: False)
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
    sub = SAMPLES / "rar3-subdirs.rar"
    solid = SAMPLES / "rar5-solid.rar"
    evil = SAMPLES / "rar5-evil-symlink-traversal.rar"
    locked = SAMPLES / "rar5-hpsw.rar"
    _, rows = _rows(tmp_path, monkeypatch, capsys, sub, solid, evil, locked)
    assert f"{sub}#att=sub/dir1/file1.txt" in rows
    assert f"{sub}#att=sub/with space/long fn.txt" in rows
    assert f"{solid}#att=stest2.txt" in rows
    assert rows[str(evil)]["needs"] == ["skipped up (symlink)"]
    assert rows[str(locked)]["needs"] == [
        "encrypted archive (meltify can't read it without the password)"
    ]


def test_streamed_archive_over_the_entry_limit_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(archive, "MAX_ENTRIES", 2)
    path = mk.evil_tar(tmp_path)
    out = archive.convert(path, Src(str(path)))
    assert out.children == [] and out.needs == ["archive rejected: over 2 entries"]
