import json
from pathlib import Path

from meltify.cli import main
from meltify.converters import pick
from meltify.converters.rtf import convert
from meltify.evidence import Src

KOREAN = (
    b"{\\rtf1\\ansi\\ansicpg949\\deff0{\\fonttbl{\\f0 Batang;}}\n"
    b"\\f0 Hello RTF\\par\n"
    b"\\'c7\\'d1\\'b1\\'db cp949 bytes\\par\n"
    b"\\uc1\\u54620?\\u44544? unicode escapes\\par\n"
    b"{\\*\\generator hidden}last line\\par\n}"
)


def test_codepage_bytes_and_unicode_escapes_decode(tmp_path):
    path = tmp_path / "a.rtf"
    path.write_bytes(KOREAN)
    (block,) = convert(path, Src("a.rtf")).blocks
    assert block.src.cite() == "a.rtf:1"
    assert block.text.splitlines() == [
        "1| Hello RTF",
        "2| 한글 cp949 bytes",
        "3| 한글 unicode escapes",
        "4| last line",
    ]


def test_rtf_without_suffix_is_sniffed(tmp_path):
    path = tmp_path / "note"
    path.write_bytes(KOREAN)
    assert pick(path)[0] == "rtf"


def test_read_cites_rtf_end_to_end(tmp_path, monkeypatch, capsys):
    (tmp_path / "a.rtf").write_bytes(KOREAN)
    monkeypatch.chdir(tmp_path)
    main(["read", "a.rtf", "--json", "--limit", "0"])
    (row,) = json.loads(capsys.readouterr().out)["results"]
    assert row["kind"] == "rtf"
    assert "## a.rtf:1\n1| Hello RTF\n2| 한글 cp949 bytes" in Path(row["out"]).read_text()
