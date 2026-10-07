from dataclasses import replace

import pytest

from meltify.evidence import MISSING, Envelope, Src, finding


@pytest.mark.parametrize(
    "src,want",
    [
        (Src("a.txt", line=42), "a.txt:42"),
        (
            Src("a.pdf", page=3, bbox=(72, 95, 140, 103.25), unit="pt"),
            "a.pdf#p3@pt(72,95,140,103.2)",
        ),
        (Src("call.m4a", t=(83.4, 87.0)), "call.m4a@00:01:23.4-00:01:27.0"),
        (Src("b.xlsx", sheet="Sheet1", cell="B7"), "b.xlsx#Sheet1!B7"),
        (Src("mail.eml", parts=("cal.xlsx",), sheet="S", cell="A1"), "mail.eml#att=cal.xlsx#S!A1"),
        (Src("menu.png", bbox=(1, 2, 3, 4)), "menu.png@px(1,2,3,4)"),
        (Src("a.mp4", t=(59.96, 3599.97)), "a.mp4@00:01:00.0-01:00:00.0"),
        (Src("a.json", jpath="$.items[0]"), "a.json#$.items[0]"),
        (Src("a.pdf", page=2), "a.pdf#p2"),
        (Src("a.xlsx", sheet="Hidden"), "a.xlsx#Hidden"),
    ],
)
def test_cite(src, want):
    assert src.cite() == want


@pytest.mark.parametrize(
    "src,want",
    [
        (Src("a.zip").inside("docs/b.pdf"), "a.zip#att=docs/b.pdf"),
        (Src("a.zip", page=9).inside("docs/b.pdf"), "a.zip#att=docs/b.pdf"),
        (replace(Src("a.zip").inside("docs/b.pdf"), page=3), "a.zip#att=docs/b.pdf#p3"),
        (
            replace(Src("a.zip").inside("x.zip").inside("c.txt"), line=1),
            "a.zip#att=x.zip#att=c.txt:1",
        ),
        (
            replace(Src("mail.eml").inside("inv.png"), bbox=(1, 2, 3, 4), unit="px"),
            "mail.eml#att=inv.png@px(1,2,3,4)",
        ),
        (Src("https://x.dev/post", anchor="install", line=28), "https://x.dev/post#install:28"),
        (Src("a.hwp", section=1, line=12), "a.hwp#s1:12"),
        (Src("a.pptx", slide=4), "a.pptx#slide4"),
        (Src("a.docx", para=12), "a.docx#para12"),
        (Src("a.pdf", page=3, img=2, bbox=(0, 0, 5, 5)), "a.pdf#p3#img2@px(0,0,5,5)"),
        (Src("a.ipynb", cell="3"), "a.ipynb#cell=3"),
        (Src("a.numbers", sheet="Sheet>Table", cell="A2"), "a.numbers#Sheet>Table!A2"),
        (Src("a.db", sheet="users", cell="rowid=5"), "a.db#users!rowid=5"),
        (
            replace(Src("export.zip").inside("general/2026-10-05.json"), ts="1759622400.000100"),
            "export.zip#att=general/2026-10-05.json#ts=1759622400.000100",
        ),
        (
            Src(
                "all",
                parts=("m",),
                jpath="$.a",
                anchor="h",
                section=1,
                page=2,
                slide=3,
                sheet="S",
                cell="B2",
                para=4,
                ts="9",
                img=5,
                bbox=(1, 2, 3, 4),
                unit="pt",
                t=(0, 1),
                line=6,
            ),
            "all#att=m#$.a#h#s1#p2#slide3#S!B2#para4#ts=9#img5@pt(1,2,3,4)@00:00:00.0-00:00:01.0:6",
        ),
    ],
)
def test_cite_new_fields_and_nesting(src, want):
    assert src.cite() == want


def test_nested_src_dict_lists_parts_outermost_first():
    src = Src("a.zip").inside("x.zip").inside("c.txt")
    assert src.to_dict() == {"path": "a.zip", "parts": ("x.zip", "c.txt")}
    assert Src("a.txt").to_dict() == {"path": "a.txt"}


def test_finding_carries_src_and_cite():
    f = finding(Src("a.txt", line=1), text="hello")
    assert f == {"text": "hello", "src": {"path": "a.txt", "line": 1}, "cite": "a.txt:1"}


def test_envelope_exit_codes():
    env = Envelope("x", "0.1.0")
    assert env.ok and env.exit_code == 0
    env.error("failed", "bad")
    assert not env.ok and env.exit_code == 1
    env.error(MISSING, "no ffmpeg", "brew install ffmpeg")
    assert env.exit_code == 3
    d = env.to_dict()
    assert d["schema"] == "meltify/v1"
    assert d["errors"][1]["hint"] == "brew install ffmpeg"
