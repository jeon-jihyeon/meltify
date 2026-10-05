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
        (Src("mail.eml", part="cal.xlsx", sheet="S", cell="A1"), "mail.eml#att=cal.xlsx#S!A1"),
        (Src("menu.png", bbox=(1, 2, 3, 4)), "menu.png@px(1,2,3,4)"),
        (Src("a.mp4", t=(59.96, 3599.97)), "a.mp4@00:01:00.0-01:00:00.0"),
    ],
)
def test_cite(src, want):
    assert src.cite() == want


def test_finding_carries_src_and_cite():
    f = finding(Src("a.txt", line=1), text="hello")
    assert f == {"text": "hello", "src": {"path": "a.txt", "line": 1}, "cite": "a.txt:1"}


def test_envelope_exit_codes():
    env = Envelope("x", "0.1.0")
    assert env.ok and env.exit_code == 0
    env.error("violation", "bad")
    assert not env.ok and env.exit_code == 1
    env.error(MISSING, "no ffmpeg", "brew install ffmpeg")
    assert env.exit_code == 3
    d = env.to_dict()
    assert d["schema"] == "meltify/v1"
    assert d["errors"][1]["hint"] == "brew install ffmpeg"
