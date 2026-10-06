import importlib.util
import json
import struct
from pathlib import Path

import pytest

from meltify import converters
from meltify.cli import main
from meltify.converters import Entry
from meltify.converters.msg import convert
from meltify.evidence import Src
from tests.fixtures.make_binary import compound_file
from tests.fixtures.make_docs import card

pytest.importorskip("olefile")

PICTURE = card("Q3 CHART 4,210", (300, 100))


def _text(value: str) -> bytes:
    return value.encode("utf-16-le")


def outlook_msg(path: Path) -> Path:
    attach = "__attach_version1.0_#0000000{}"
    path.write_bytes(
        compound_file(
            {
                "__properties_version1.0": b"\0" * 32,
                "__substg1.0_0037001F": _text("Q3 numbers"),
                "__substg1.0_0C1F001F": _text("cfo@example.com"),
                "__substg1.0_1000001F": _text("Revenue grew 12 percent, chart inline."),
                "__recip_version1.0_#00000000": {"__substg1.0_3001001F": _text("Team")},
                attach.format(0): {
                    "__substg1.0_3707001F": _text("notes.txt"),
                    "__substg1.0_37010102": b"line one\n" * 600,
                },
                attach.format(1): {
                    "__substg1.0_3707001F": _text("chart.png"),
                    "__substg1.0_3712001F": _text("chart@mail"),
                    "__substg1.0_370E001F": _text("image/png"),
                    "__substg1.0_37010102": PICTURE,
                },
                attach.format(2): {
                    "__substg1.0_3707001F": _text("C:\\mail\\fwd.msg"),
                    "__substg1.0_3701000D": {
                        "__substg1.0_0037001F": _text("Old thread"),
                        "__substg1.0_1000001F": _text("Forwarded body text"),
                        attach.format(0): {"__substg1.0_37010102": b"x"},
                    },
                },
                attach.format(3): {"__substg1.0_3704001E": b"EMPTY.BIN\0"},
            }
        )
    )
    return path


def test_attachments_become_children_and_inline_pictures_ocr_jobs(tmp_path):
    out = convert(outlook_msg(tmp_path / "a.msg"), Src("a.msg"))
    assert out.kind == "mail"
    assert [(c.name, len(c.data)) for c in out.children] == [("notes.txt", 5400)]
    assert [(j.src.cite(), j.data) for j in out.jobs] == [("a.msg#img1", PICTURE)]
    assert [b.src.cite() for b in out.blocks] == ["a.msg", "a.msg#att=fwd.msg"]
    assert out.blocks[0].text.endswith("Attachments: notes.txt, chart.png, fwd.msg, EMPTY.BIN")
    assert out.blocks[1].text == "Old thread\n\nForwarded body text"
    assert sorted(n for n in out.needs if n != "markitdown") == [
        "1 attachment in fwd.msg not read",
        "empty or unreadable attachment EMPTY.BIN",
    ]


@pytest.mark.skipif(importlib.util.find_spec("markitdown") is None, reason="needs markitdown")
def test_body_text_comes_from_markitdown(tmp_path, monkeypatch):
    import markitdown

    # The msg converter runs straight, without the wrapper's file type guessing
    monkeypatch.setattr(markitdown, "MarkItDown", lambda *a, **k: pytest.fail("built MarkItDown"))
    out = convert(outlook_msg(tmp_path / "a.msg"), Src("a.msg"))
    text = out.blocks[0].text
    assert "Q3 numbers" in text and "Revenue grew 12 percent" in text


def test_read_melts_msg_attachments_end_to_end(tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(
        converters.SUFFIXES, ".msg", Entry("mail", "meltify.converters.msg:convert")
    )
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    outlook_msg(tmp_path / "a.msg")
    monkeypatch.chdir(tmp_path)
    main(["read", "a.msg", "--json", "--limit", "0", "--shallow"])
    rows = {r["cite"]: r for r in json.loads(capsys.readouterr().out)["results"]}
    assert rows["a.msg#att=notes.txt"]["kind"] == "text"
    assert "## a.msg#att=fwd.msg\nOld thread" in Path(rows["a.msg"]["out"]).read_text()


def _properties(header: int, codepage: int | None) -> bytes:
    entry = b"" if codepage is None else struct.pack("<IIi4x", 0x3FFD0003, 0, codepage)
    return b"\0" * header + entry


@pytest.mark.parametrize(
    ("codepage", "name"), [(949, "보고서.txt"), (1251, "отчет.txt"), (None, "보고서.txt")]
)
def test_ansi_names_decode_with_the_message_code_page(tmp_path, codepage, name):
    encoding = "cp949" if codepage is None else f"cp{codepage}"
    path = tmp_path / "a.msg"
    path.write_bytes(
        compound_file(
            {
                "__properties_version1.0": _properties(32, codepage),
                "__attach_version1.0_#00000000": {
                    "__substg1.0_3707001E": name.encode(encoding) + b"\0",
                    "__substg1.0_37010102": b"body",
                },
            }
        )
    )
    out = convert(path, Src("a.msg"))
    assert [c.name for c in out.children] == [name]
