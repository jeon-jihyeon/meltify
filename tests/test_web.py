from __future__ import annotations

import base64
import json
from pathlib import Path

from meltify.cli import main
from meltify.converters import Entry
from meltify.converters.web import convert, convert_page, data_uri
from meltify.engines.ocr import LOCAL, TextBox
from meltify.evidence import Src
from tests.fixtures.make_docs import card


def ocr_reader(text: str):
    """An OCR engine class that reads `text` from any picture"""

    class Reader:
        kind = LOCAL

        def __init__(self, name: str) -> None:
            self.name = name

        def missing(self):
            return None

        def recognize(self, image, size):
            w, h = size
            return [TextBox(text, (0.1 * w, 0.1 * h, 0.5 * w, 0.2 * h), 0.9)]

    return Reader


FIXTURES = Path(__file__).parent / "fixtures" / "web"


def page(name: str, **kw):
    return convert_page(FIXTURES / name, Src(f"https://example.org/{name}"), **kw)


def lines_of(out) -> dict[int, str]:
    rows = {}
    for block in out.blocks:
        for line in block.text.splitlines():
            n, _, text = line.partition("| ")
            rows[int(n)] = text
    return rows


def test_headings_cite_their_html_ids() -> None:
    out = page("docs.html")
    cites = [b.src.cite() for b in out.blocks]
    base = "https://example.org/docs.html"
    assert cites[0] == f"{base}#module-widget:1"
    assert f"{base}#installation:5" in cites
    assert f"{base}#configuration:15" in cites
    # Two headings called "Example" keep their own ids in order
    assert [c.split("#")[1].split(":")[0] for c in cites if "#example" in c] == [
        "example",
        "example-1",
    ]
    # A heading without any id falls back to the plain line cite
    assert cites[-1] == f"{base}:28"
    rows = lines_of(out)
    assert rows[28] == "## Changelog"
    assert rows[5] == "## Installation"


def test_tables_are_kept_and_links_dropped() -> None:
    text = "\n".join(lines_of(page("docs.html")).values())
    assert "| timeout | 30 |" in text
    assert "](" not in text


def test_footer_copyright_and_ai_notice_are_kept() -> None:
    docs = "\n".join(lines_of(page("docs.html")).values())
    assert "© Copyright 2026 Example Docs Team." in docs
    assert "This page is licensed under the Example Documentation License." in docs
    news = page("news.html")
    text = "\n".join(lines_of(news).values())
    assert "AI 학습 및 활용 금지" in text
    # A menu link alone isn't a notice
    assert "저작권규약" not in text
    assert [b.src.cite() for b in news.blocks] == [
        "https://example.org/news.html:1",
        "https://example.org/news.html#reaction:7",
    ]


def test_script_shell_page_needs_render() -> None:
    assert page("shell.html").needs == ["render"]
    assert page("docs.html").needs == []


def test_whole_page_keeps_navigation() -> None:
    out = page("docs.html", whole=True)
    text = "\n".join(lines_of(out).values())
    assert "Home" in text and "Changelog" in text
    assert "render(data)" not in text


def _site(root: Path) -> Path:
    """A saved page with one picture of each kind it can point at"""
    site = root / "site"
    (site / "img").mkdir(parents=True)
    (site / "img" / "chart.png").write_bytes(card("Q3 4,210", (300, 100)))
    (root / "secret.png").write_bytes(card("SECRET", (300, 100)))
    inline = base64.b64encode(card("INLINE 77", (300, 100))).decode()
    page = site / "page.html"
    page.write_text(
        "<html><body><h1>Report</h1>"
        f'<img src="data:image/png;base64,{inline}">'
        '<h2 id="sales">Sales</h2><p>Numbers follow</p>'
        '<img src="img/chart.png?v=2">'
        '<img src="https://cdn.example.org/a.png">'
        '<img src="../secret.png">'
        '<img src="nope.png">'
        '<img src="data:image/svg+xml,%3Csvg%2F%3E">'
        '<img src="data:image/gif;base64,R0lGODlhAQABAAAAACw=" data-src="img/chart.png">'
        "<img>"
        "</body></html>"
    )
    return page


def test_local_page_reads_pictures_inside_its_folder_only(tmp_path) -> None:
    out = convert(_site(tmp_path), Src("page.html"))
    assert out.kind == "web"
    assert any("Numbers follow" in b.text for b in out.blocks)
    assert [j.src.cite() for j in out.jobs] == [
        "page.html#img1",
        "page.html#sales#img2",
        "page.html#sales#img7",
    ]
    assert out.jobs[0].data == card("INLINE 77", (300, 100))
    assert sorted(out.needs) == [
        "1 missing image not read",
        "1 out-of-folder image not read",
        "1 remote image not read",
    ]
    # An SVG keeps its text, so it goes on to the svg converter instead of OCR
    assert [(c.name, c.data, c.parent.cite()) for c in out.children] == [
        ("inline.svg", b"<svg/>", "page.html#sales#img6")
    ]


def test_symlinked_picture_out_of_the_folder_is_refused(tmp_path) -> None:
    (tmp_path / "secret.png").write_bytes(card("SECRET", (300, 100)))
    site = tmp_path / "site"
    site.mkdir()
    (site / "link.png").symlink_to(tmp_path / "secret.png")
    (site / "page.html").write_text('<p>x</p><img src="link.png">')
    out = convert(site / "page.html", Src("page.html"))
    assert out.jobs == []
    assert out.needs == ["1 out-of-folder image not read"]


def test_fetched_page_reads_only_inline_pictures(tmp_path) -> None:
    inline = base64.b64encode(card("INLINE 77", (300, 100))).decode()
    page = tmp_path / "page.html"
    page.write_text(
        f'<h1 id="top">T</h1><img src="data:image/png;base64,{inline}"><img src="local.png">'
    )
    (tmp_path / "local.png").write_bytes(card("LOCAL", (300, 100)))
    out = convert_page(page, Src("https://example.org/p"))
    assert [j.src.cite() for j in out.jobs] == ["https://example.org/p#top#img1"]
    # A fetched page's relative picture lives on the server, never on this disk
    assert out.needs == ["1 remote image not read"]


def test_data_uris_decode_or_say_why_not() -> None:
    assert data_uri("data:text/plain,a%20b") == ("inline.txt", b"a b")
    assert data_uri("data:image/png;base64,!!!") == "unreadable image"
    assert data_uri("data:image/png;base64") == "unreadable image"


def test_read_cites_local_page_pictures_end_to_end(tmp_path, monkeypatch, capsys) -> None:
    from meltify import converters
    from meltify.engines import ocr

    reader = ocr_reader("Q3 4,210")
    monkeypatch.setattr(ocr, "select", lambda spec, s: [reader("vision"), reader("paddle")])
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setitem(
        converters.SUFFIXES, ".html", Entry("web", "meltify.converters.web:convert")
    )
    site = tmp_path / "site"
    (site / "img").mkdir(parents=True)
    (site / "img" / "chart.png").write_bytes(card("Q3 4,210", (300, 100)))
    (site / "page.html").write_text('<h2 id="sales">Sales</h2><img src="img/chart.png">')
    monkeypatch.chdir(site)
    main(["read", "page.html", "--json", "--limit", "0"])
    (row,) = json.loads(capsys.readouterr().out)["results"]
    assert row["kind"] == "web" and row["needs"] == []
    md = Path(row["out"]).read_text()
    assert "## page.html#sales#img1\n" in md and "Q3 4,210" in md


def test_short_page_keeps_its_text_and_a_script_shell_still_asks_for_render(tmp_path) -> None:
    page = tmp_path / "p.html"
    page.write_text("<html><body><p>Closed on Monday</p><script>app()</script></body></html>")
    out = convert_page(page, Src("https://example.org/p"))
    assert "Closed on Monday" in "\n".join(b.text for b in out.blocks)
    assert out.needs == ["render"]


def test_local_svgs_with_the_same_file_name_stay_apart(tmp_path) -> None:
    for folder in ("a", "b"):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "logo.svg").write_text(f"<svg><text>{folder} logo</text></svg>")
    page = tmp_path / "page.html"
    page.write_text('<p>x</p><img src="a/logo.svg"><img src="b/logo.svg">')
    out = convert(page, Src("page.html"))
    assert [c.name for c in out.children] == ["a/logo.svg", "b/logo.svg"]


def test_a_bad_local_reference_is_listed_and_the_page_still_melts(tmp_path) -> None:
    (tmp_path / "ok.png").write_bytes(card("FINE 12", (300, 100)))
    page = tmp_path / "page.html"
    page.write_text('<p>Body text</p><img src="%00.png"><img src="ok.png">')
    out = convert(page, Src("page.html"))
    assert any("Body text" in b.text for b in out.blocks)
    assert [j.src.cite() for j in out.jobs] == ["page.html#img2"]
    assert out.needs == ["1 bad image reference not read"]


def test_a_picture_shown_many_times_is_read_and_decoded_once(tmp_path, monkeypatch) -> None:
    from meltify.converters import web

    picture = card("LOGO 42", (300, 100))
    (tmp_path / "logo.png").write_bytes(picture)
    inline = f'<img src="data:image/png;base64,{base64.b64encode(picture).decode()}">'
    page = tmp_path / "page.html"
    page.write_text("<p>x</p>" + '<img src="logo.png"><img src="./logo.png">' * 20 + inline * 20)
    reads, decodes = [], []
    read_bytes, decode = Path.read_bytes, web.data_uri
    monkeypatch.setattr(Path, "read_bytes", lambda p: reads.append(p.name) or read_bytes(p))
    monkeypatch.setattr(web, "data_uri", lambda ref: decodes.append(ref) or decode(ref))
    out = convert(page, Src("page.html"))
    assert reads.count("logo.png") == 1 and len(decodes) == 1
    assert len(out.jobs) == 60
    # One copy in memory, however many places cite it
    assert len({id(j.data) for j in out.jobs}) == 1


def test_local_pictures_past_the_page_total_are_needs_and_never_read(tmp_path, monkeypatch) -> None:
    pictures = [card(f"CHART {i}", (300, 100)) for i in range(3)]
    for i, picture in enumerate(pictures):
        (tmp_path / f"{i}.png").write_bytes(picture)
    page = tmp_path / "page.html"
    page.write_text('<p>x</p><img src="0.png"><img src="1.png"><img src="2.png"><img src="2.png">')
    # Room for the first two pictures only
    room = len(pictures[0]) + len(pictures[1])
    monkeypatch.setattr("meltify.converters.embeds.MAX_PICTURE_BYTES", room)
    reads = []
    read_bytes = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda p: reads.append(p.name) or read_bytes(p))
    out = convert(page, Src("page.html"))
    assert [j.data for j in out.jobs] == pictures[:2]
    assert out.needs == ["2 images not read (over the 256 MiB picture total)"]
    assert "2.png" not in reads
