import struct
import sys
from pathlib import Path

import pytest

from meltify.converters import wordperfect
from meltify.evidence import Src
from meltify.passwords import Locked

WALKED = wordperfect.WALKED.format(hint=wordperfect.HINT)


def _wpc(body: bytes, major: int, encrypted: bool = False) -> bytes:
    """A WPC header pointing at a document area right after a little prefix padding"""
    start = 32
    header = b"\xffWPC" + struct.pack("<IBBBBH2x", start, 1, 0x0A, major, 0, 0x1234 * encrypted)
    return header.ljust(start, b"\0") + body


def _wp6_group(code: int, sub: int, payload: bytes) -> bytes:
    # The size counts the whole group, code bytes included
    size = len(payload) + 7
    return (
        bytes([code, sub])
        + struct.pack("<H", size)
        + payload
        + struct.pack("<H", size)
        + bytes([code])
    )


def _wp5_group(code: int, sub: int, payload: bytes) -> bytes:
    size = len(payload) + 4
    head = bytes([code, sub]) + struct.pack("<H", size)
    return head + payload + struct.pack("<H", size) + bytes([sub, code])


WP6 = (
    b"Hello\x80world"
    + _wp6_group(0xD0, 0x04, b"\0\0")  # hard end of line
    + b"Caf\xf0\x29\x01\xf0\xcc"  # an e with acute from character set 1, then a hard return
    + _wp6_group(0xE0, 0x11, b"\0\0")  # tab
    + b"Total\xf2\x0c\xf2"  # bold on
    + _wp6_group(0xD4, 0x1A, b"HIDDEN CODE")
    # An a with acute from the low bytes, a soft space, then a math symbol
    + b"\x07\x80\xf0\x05\x09\xf0"
)
WP5 = (
    b"Dear Bob,\x0aGr\xc0\x29\x01\xc0at"
    + b"\x0d"  # soft return
    + b"\xc1"
    + b"\0" * 7
    + b"\xc1"  # tab
    + b"news"
    + _wp5_group(0xD1, 0x01, b"FONT NAME")
    + b"\xa9ok\x0a"
)


def _file(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _no_tools(monkeypatch) -> None:
    monkeypatch.setattr(wordperfect, "find", lambda name: None)
    monkeypatch.setattr(wordperfect, "soffice", lambda: None)


def test_wp6_text_walks_past_function_codes(tmp_path, monkeypatch):
    _no_tools(monkeypatch)
    path = _file(tmp_path, "memo.wpd", _wpc(WP6, major=2))
    out = wordperfect.convert(path, Src("memo.wpd"))
    assert [(b.src.cite(), b.text) for b in out.blocks] == [
        ("memo.wpd:1", "1| Hello world\n2| Café\n3| \tTotalá \ufffd")
    ]
    assert out.needs == [WALKED, "1 WordPerfect special character not mapped"]


def test_wp5_text_walks_past_function_codes(tmp_path, monkeypatch):
    _no_tools(monkeypatch)
    path = _file(tmp_path, "letter.wp5", _wpc(WP5, major=0))
    out = wordperfect.convert(path, Src("letter.wp5"))
    assert [b.text for b in out.blocks] == ["1| Dear Bob,\n2| Gréat \tnews-ok"]
    assert out.needs == [WALKED]


def test_wpd2text_wins_when_installed(tmp_path, monkeypatch):
    tool = _file(
        tmp_path,
        "wpd2text",
        f"#!{sys.executable}\nprint('Hello from libwpd\\nline two')\n".encode(),
    )
    tool.chmod(0o755)
    monkeypatch.setattr(wordperfect, "find", lambda name: str(tool))
    path = _file(tmp_path, "memo.wpd", _wpc(WP6, major=2))
    out = wordperfect.convert(path, Src("memo.wpd"))
    assert [b.text for b in out.blocks] == ["1| Hello from libwpd\n2| line two"]
    assert out.needs == []


def test_a_file_named_like_an_option_reaches_wpd2text_as_a_path(tmp_path, monkeypatch):
    # The tool echoes its arguments, so the test sees exactly what it was given
    tool = _file(
        tmp_path,
        "wpd2text",
        f"#!{sys.executable}\nimport sys\nprint(repr(sys.argv[1:]))\n".encode(),
    )
    tool.chmod(0o755)
    monkeypatch.setattr(wordperfect, "find", lambda name: str(tool))
    path = _file(tmp_path, "--accept=socket,host=0.0.0.0,port=2002;urp;.wpd", _wpc(WP6, major=2))
    monkeypatch.chdir(tmp_path)
    out = wordperfect.convert(Path(path.name), Src(path.name))
    assert out.blocks[0].text == f"1| {[str(path.resolve())]!r}"


def test_failing_wpd2text_falls_back_to_the_walker(tmp_path, monkeypatch):
    tool = _file(tmp_path, "wpd2text", f"#!{sys.executable}\nraise SystemExit(1)\n".encode())
    tool.chmod(0o755)
    monkeypatch.setattr(wordperfect, "find", lambda name: str(tool))
    monkeypatch.setattr(wordperfect, "soffice", lambda: None)
    path = _file(tmp_path, "memo.wpd", _wpc(WP6, major=2))
    out = wordperfect.convert(path, Src("memo.wpd"))
    assert out.blocks[0].text.startswith("1| Hello world")
    # wpd2text is installed, so only LibreOffice is left to suggest
    assert out.needs[0] == wordperfect.WALKED.format(hint="wpd2text failed, install LibreOffice")


def test_libreoffice_goes_before_the_walker(tmp_path, monkeypatch):
    def convert_to(path, target, out_dir, timeout):
        txt = out_dir / "memo.txt"
        txt.write_text("Hello from LibreOffice\nFootnote text")
        return txt

    monkeypatch.setattr(wordperfect, "find", lambda name: None)
    monkeypatch.setattr(wordperfect, "soffice", lambda: "/opt/soffice")
    monkeypatch.setattr(wordperfect, "convert_to", convert_to)
    path = _file(tmp_path, "memo.wpd", _wpc(WP6, major=2))
    out = wordperfect.convert(path, Src("memo.wpd"))
    assert [b.text for b in out.blocks] == ["1| Hello from LibreOffice\n2| Footnote text"]
    assert out.needs == []


def test_failing_libreoffice_falls_back_to_the_walker_and_says_so(tmp_path, monkeypatch):
    def convert_to(path, target, out_dir, timeout):
        raise RuntimeError("soffice wrote no .txt")

    monkeypatch.setattr(wordperfect, "find", lambda name: None)
    monkeypatch.setattr(wordperfect, "soffice", lambda: "/opt/soffice")
    monkeypatch.setattr(wordperfect, "convert_to", convert_to)
    path = _file(tmp_path, "memo.wpd", _wpc(WP6, major=2))
    out = wordperfect.convert(path, Src("memo.wpd"))
    assert out.blocks[0].text.startswith("1| Hello world")
    hint = "LibreOffice failed, install libwpd for wpd2text"
    assert out.needs[0] == wordperfect.WALKED.format(hint=hint)


def test_password_protected_wordperfect_is_a_need(tmp_path, monkeypatch):
    _no_tools(monkeypatch)
    path = _file(tmp_path, "locked.wpd", _wpc(WP6, major=2, encrypted=True))
    with pytest.raises(Locked) as e:
        wordperfect.convert(path, Src("locked.wpd"))
    assert str(e.value) == "encrypted, can't decrypt (WordPerfect passwords aren't supported)"


def test_wordperfect_without_a_header_names_the_tools(tmp_path, monkeypatch):
    _no_tools(monkeypatch)
    # WordPerfect 4.2 and older start straight with text and codes
    path = _file(tmp_path, "old.wp", b"\xcb\x0a\x01Old letter")
    out = wordperfect.convert(path, Src("old.wp"))
    assert out.blocks == []
    assert out.needs == [f"WordPerfect file without a WPC header not read ({wordperfect.HINT})"]


def test_wordperfect_without_a_header_goes_to_libreoffice(tmp_path, monkeypatch):
    calls = []

    def convert_to(path, target, out_dir, timeout):
        calls.append(target)
        txt = out_dir / "old.txt"
        txt.write_text("Old letter\nSigned")
        return txt

    monkeypatch.setattr(wordperfect, "find", lambda name: None)
    monkeypatch.setattr(wordperfect, "soffice", lambda: "/opt/soffice")
    monkeypatch.setattr(wordperfect, "convert_to", convert_to)
    path = _file(tmp_path, "old.wp", b"\xcb\x0a\x01Old letter")
    out = wordperfect.convert(path, Src("old.wp"))
    assert [b.text for b in out.blocks] == ["1| Old letter\n2| Signed"]
    assert out.needs == [] and calls == ["txt:Text (encoded):UTF8"]


def test_unknown_version_is_a_need(tmp_path, monkeypatch):
    _no_tools(monkeypatch)
    path = _file(tmp_path, "mac.wpd", _wpc(b"text", major=3))
    out = wordperfect.convert(path, Src("mac.wpd"))
    assert out.needs == [f"WordPerfect version 3.0 not read ({wordperfect.HINT})"]


def test_sniff_knows_the_wpc_header():
    assert wordperfect.sniff(Path("x"), _wpc(b"", major=2))
    assert not wordperfect.sniff(Path("x"), b"%PDF-")
