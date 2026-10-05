from __future__ import annotations

from pathlib import Path

from meltify.converters.web import convert_page
from meltify.evidence import Src

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
