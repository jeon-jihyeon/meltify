import io
import json
import plistlib
from pathlib import Path

import pytest
from PIL import Image

from meltify import converters
from meltify.cli import main
from meltify.converters import Entry, webarchive
from meltify.evidence import Src

BODY = "Saved pages keep their text when Safari archives them. " * 8


def _png(size=(120, 80)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, "white").save(buf, "PNG")
    return buf.getvalue()


def _archive(main: dict, subresources=(), frames=()) -> dict:
    return {
        "WebMainResource": main,
        "WebSubresources": list(subresources),
        "WebSubframeArchives": list(frames),
    }


def _html(text: str, encoding: str = "utf-8", declared: str | None = None) -> dict:
    page = f"<html><head><title>Saved</title></head><body><h1>Notes</h1><p>{text}</p></body></html>"
    resource = {
        "WebResourceData": page.encode(encoding),
        "WebResourceMIMEType": "text/html",
        "WebResourceURL": "https://example.org/notes.html",
    }
    if declared:
        resource["WebResourceTextEncodingName"] = declared
    return resource


def _write(tmp_path: Path, archive: dict, name: str = "page.webarchive") -> Path:
    path = tmp_path / name
    path.write_bytes(plistlib.dumps(archive, fmt=plistlib.FMT_BINARY))
    return path


def test_page_text_cites_lines_and_images_become_jobs(tmp_path):
    # Headings come through as markdown only with the office extra
    pytest.importorskip("markitdown")
    images = [
        {
            "WebResourceData": _png(),
            "WebResourceMIMEType": "image/png",
            "WebResourceURL": "https://example.org/a.png",
        },
        {
            "WebResourceData": b"body{}",
            "WebResourceMIMEType": "text/css",
            "WebResourceURL": "https://example.org/s.css",
        },
        {
            "WebResourceData": b"<svg/>",
            "WebResourceMIMEType": "image/svg+xml",
            "WebResourceURL": "https://example.org/i.svg",
        },
    ]
    path = _write(tmp_path, _archive(_html(BODY), images))
    out = webarchive.convert(path, Src(str(path)))
    assert out.kind == "webarchive"
    text = "\n".join(b.text for b in out.blocks)
    assert "# Notes" in text and "Safari archives them" in text
    assert out.blocks[0].src.line == 1
    # The page doesn't reference it, so it cites its name in the archive
    assert [j.src.cite() for j in out.jobs] == [f"{path}#att=a.png"]
    assert out.jobs[0].data == images[0]["WebResourceData"]
    # An SVG keeps its text, so it melts through the svg converter instead of OCR
    assert out.needs == []
    assert [(c.name, c.data, c.parent.cite()) for c in out.children] == [
        ("i.svg", b"<svg/>", str(path))
    ]


def test_pictures_the_page_shows_cite_their_place_and_are_not_remote(tmp_path):
    main = _html(BODY)
    main["WebResourceData"] = main["WebResourceData"].replace(
        b"</p>", b'</p><h2 id="fig">Figure</h2><img src="img/a.png#x"><img src="//cdn.org/b.png">'
    )
    picture = {
        "WebResourceData": _png(),
        "WebResourceMIMEType": "image/png",
        "WebResourceURL": "https://example.org/img/a.png",
    }
    path = _write(tmp_path, _archive(main, [picture]))
    out = webarchive.convert(path, Src("page.webarchive"))
    assert [j.src.cite() for j in out.jobs] == ["page.webarchive#fig#img1"]
    # Only the picture Safari didn't save is remote
    assert out.needs == ["1 remote image not read"]


def test_a_short_page_keeps_its_text(tmp_path):
    path = _write(tmp_path, _archive(_html("Just one line")))
    out = webarchive.convert(path, Src("page.webarchive"))
    assert "Just one line" in "\n".join(b.text for b in out.blocks)


def test_a_saved_page_keeps_its_navigation_and_footer(tmp_path):
    main = _html(BODY)
    main["WebResourceData"] = (
        main["WebResourceData"]
        .replace(b"<body>", b"<body><nav>Pricing and contact links</nav>")
        .replace(b"</body>", b"<footer>Office hours are 9 to 5</footer></body>")
    )
    path = _write(tmp_path, _archive(main))
    text = "\n".join(b.text for b in webarchive.convert(path, Src("page.webarchive")).blocks)
    assert "Pricing and contact links" in text and "Office hours are 9 to 5" in text


def test_served_encoding_wins_over_the_meta_tag(tmp_path):
    korean = "웹 아카이브에 저장된 한국어 본문 문장. " * 10
    main = _html(korean, encoding="euc-kr", declared="EUC-KR")
    main["WebResourceData"] = main["WebResourceData"].replace(
        b"<head>", b'<head><meta charset="utf-8">'
    )
    path = _write(tmp_path, _archive(main))
    out = webarchive.convert(path, Src(str(path)))
    assert "한국어 본문 문장" in "\n".join(b.text for b in out.blocks)


def test_frames_and_non_html_main_resources_become_children(tmp_path):
    frame = _archive(_html("Frame text " * 30))
    path = _write(tmp_path, _archive(_html(BODY), frames=[frame]))
    out = webarchive.convert(path, Src(str(path)))
    assert [c.name for c in out.children] == ["frame1.webarchive"]
    assert plistlib.loads(out.children[0].data)["WebMainResource"]["WebResourceURL"].endswith(
        "notes.html"
    )

    image = {
        "WebResourceData": _png(),
        "WebResourceMIMEType": "image/png",
        "WebResourceURL": "https://example.org/photo",
    }
    path = _write(tmp_path, _archive(image), "image.webarchive")
    out = webarchive.convert(path, Src(str(path)))
    assert out.blocks == [] and [c.name for c in out.children] == ["photo.png"]


def test_a_frame_referenced_many_times_is_melted_once(tmp_path):
    # Binary plists share objects, so a small file can name one frame at every level
    inner = _archive(_html("Frame text " * 30))
    middle = _archive(_html("Middle"), frames=[inner] * 20)
    path = _write(tmp_path, _archive(_html(BODY), frames=[middle] * 20))
    out = webarchive.convert(path, Src(str(path)))
    assert [c.name for c in out.children] == ["frame1.webarchive"]
    child = tmp_path / "frame1.webarchive"
    child.write_bytes(out.children[0].data)
    assert [c.name for c in webarchive.convert(child, Src("c")).children] == ["frame1.webarchive"]
    assert out.needs == []


def test_frames_sharing_one_big_resource_stop_at_the_archive_size(tmp_path):
    big = b"x" * 200_000
    frames = [
        _archive(
            {"WebResourceData": big, "WebResourceMIMEType": "text/plain", "WebResourceURL": f"u{i}"}
        )
        for i in range(10)
    ]
    path = _write(tmp_path, _archive(_html(BODY), frames=frames))
    out = webarchive.convert(path, Src(str(path)))
    assert sum(len(c.data) for c in out.children) <= 2 * path.stat().st_size
    assert len(out.children) == 2
    assert out.needs == ["8 frames not read (more frame data than the archive holds)"]


def test_frames_past_the_count_limit_are_needs(tmp_path, monkeypatch):
    monkeypatch.setattr(webarchive, "MAX_FRAMES", 2)
    frames = [_archive(_html(f"Frame {i}")) for i in range(5)]
    path = _write(tmp_path, _archive(_html(BODY), frames=frames))
    out = webarchive.convert(path, Src(str(path)))
    assert [c.name for c in out.children] == ["frame1.webarchive", "frame2.webarchive"]
    assert out.needs == ["3 frames not read (over 2 frames)"]


@pytest.mark.parametrize(
    ("data", "need"),
    [
        (b"not a plist", "unreadable webarchive"),
        (plistlib.dumps({"WebSubresources": []}), "webarchive has no main resource"),
    ],
)
def test_broken_archives_say_why(tmp_path, data, need):
    path = tmp_path / "bad.webarchive"
    path.write_bytes(data)
    out = webarchive.convert(path, Src(str(path)))
    assert out.blocks == [] and out.needs[0].startswith(need)


def test_read_melts_frames_with_member_cites(tmp_path, monkeypatch, capsys):
    entry = Entry("webarchive", "meltify.converters.webarchive:convert")
    monkeypatch.setitem(converters.SUFFIXES, ".webarchive", entry)
    frame = _archive(_html("Inner frame sentence that is long enough to keep. " * 6))
    path = _write(tmp_path, _archive(_html(BODY), frames=[frame]))
    monkeypatch.chdir(tmp_path)
    main(["read", str(path), "--json", "--limit", "0"])
    rows = {r["cite"]: r for r in json.loads(capsys.readouterr().out)["results"]}
    assert rows[str(path)]["kind"] == "webarchive"
    inner = rows[f"{path}#att=frame1.webarchive"]
    assert "Inner frame sentence" in Path(inner["out"]).read_text(encoding="utf-8")
