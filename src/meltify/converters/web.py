"""Readable text of a saved web page, cited by heading anchor and line

`src.path` is the page URL, so a cite looks like `https://x.org/doc#install:28`, where
28 is the line in the extracted text and `install` is the id of the heading above it
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import lxml.etree
import lxml.html
import trafilatura

from meltify.converters import Converted
from meltify.converters.text import numbered_lines
from meltify.evidence import Src

# Below this much text a page that runs scripts probably builds its content in the browser
MIN_TEXT = 200
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
NOTICE = re.compile(
    r"©|\(c\)|copyright|all rights reserved|licensed under|creative commons|"
    r"저작권|무단\s*(전재|복제)|재배포|"
    r"AI\s*(학습|활용)|\bTDM\b|text and data mining|(train|training)\s+(AI|artificial|machine)",
    re.I,
)
HEADS = ("h1", "h2", "h3", "h4", "h5", "h6")
TEXT_NODES = "//text()[not(ancestor::script or ancestor::style or ancestor::pre or ancestor::code)]"
INLINE = {"a", "span", "em", "strong", "b", "i", "small", "u", "font", "sup", "sub"}
NOTICE_META = ("copyright", "rights", "dcterms.rights", "tdm-reservation", "tdm-policy")


def _norm(text: str) -> str:
    return re.sub(r"\W+", "", text.replace("¶", "")).lower()


def _heading_ids(tree) -> list[tuple[str, str]]:
    """Normalized heading text and the nearest id, in document order"""
    found = []
    for h in tree.iter(*HEADS):
        hid = h.get("id") or next((e.get("id") for e in h.iterdescendants() if e.get("id")), None)
        parent = h.getparent()
        # Sphinx and many doc tools put the id on the section that wraps the heading,
        # which names only the first heading inside it
        wrapped = parent is not None and parent.tag in ("section", "div", "article")
        if not hid and wrapped and next((c for c in parent if c.tag in HEADS), None) is h:
            hid = parent.get("id")
        if hid:
            found.append((_norm(h.text_content()), hid))
    return found


def _anchor(title: str, ids: list[tuple[str, str]], used: set[int]) -> str | None:
    key = _norm(title)
    if not key:
        return None
    # The first unused match in order, so repeated headings like "Example" each get their own
    for i, (text, hid) in enumerate(ids):
        if i not in used and text == key:
            used.add(i)
            return hid
    return None


def _notices(tree, body: str) -> list[str]:
    """Copyright and AI-use lines the extractor dropped, such as a footer"""
    lines: list[str] = []
    for meta in tree.iter("meta"):
        name = (meta.get("name") or meta.get("property") or "").lower()
        content = (meta.get("content") or "").strip()
        noai = name == "robots" and re.search(r"\bno(ai|imageai)\b", content, re.I)
        if content and (name in NOTICE_META or noai):
            lines.append(f"{name}: {content}")
    have = _norm(body)
    for node in tree.xpath(TEXT_NODES):
        if not NOTICE.search(node):
            continue
        block = node.getparent()
        while block is not None and block.tag in INLINE:
            block = block.getparent()
        if block is None:
            continue
        # A footer often holds several notices split by <br>, so take only the matching lines
        for raw in block.text_content().splitlines():
            line = " ".join(raw.split())
            key = _norm(line)
            if not key or len(line) > 300 or not NOTICE.search(line) or key in have:
                continue
            # A bare link such as "Copyright" or "저작권규약" in a menu is navigation
            owner = node.getparent()
            if node.is_text and owner.tag == "a" and _norm(owner.text_content()) == key:
                continue
            if all(key not in _norm(s) for s in lines):
                lines.append(line)
    return lines


def _whole(html: str, url: str, tree) -> str:
    try:
        from markitdown.converters import HtmlConverter
    except ImportError:
        HtmlConverter = None  # noqa: N806
    if HtmlConverter is not None:
        return HtmlConverter().convert_string(html, url=url).markdown
    for el in tree.xpath("//script|//style|//noscript|//template"):
        el.drop_tree()
    text = tree.text_content()
    return re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


def convert_page(path: Path, src: Src, *, whole: bool = False) -> Converted:
    raw = path.read_bytes()
    out = Converted("web")
    try:
        tree = lxml.html.fromstring(raw)
    except (lxml.etree.ParserError, ValueError):
        out.needs.append("empty page")
        return out
    if whole:
        html = lxml.html.tostring(tree, encoding="unicode")
        text = _whole(html, src.path, lxml.html.fromstring(raw))
        extracted = len(re.sub(r"\s+", "", text))
    else:
        text = (
            trafilatura.extract(
                raw,
                url=src.path,
                output_format="markdown",
                include_tables=True,
                include_links=False,
                include_images=False,
                include_comments=False,
            )
            or ""
        )
        extracted = len(re.sub(r"\s+", "", text))
        title = (tree.findtext(".//title") or "").strip()
        first = text.lstrip().splitlines()[:1]
        if title and not (first and first[0].startswith("#")):
            text = f"# {title}\n\n{text}"
    notices = _notices(tree, text)
    if notices:
        text = text.rstrip() + "\n\n" + "\n".join(notices)

    ids = _heading_ids(tree)
    used: set[int] = set()
    sections: list[tuple[str | None, list[tuple[int, str]]]] = [(None, [])]
    for n, line in enumerate(text.splitlines(), start=1):
        if m := HEADING.match(line):
            sections.append((_anchor(m.group(2), ids, used), []))
            # Doc generators append a ¶ permalink to every heading
            line = line.rstrip("¶ ")
        sections[-1][1].append((n, line))
    for anchor, lines in sections:
        if any(line.strip() for _, line in lines):
            out.blocks += numbered_lines(lines, replace(src, anchor=anchor))

    if extracted < MIN_TEXT and b"<script" in raw.lower():
        out.needs.append("render")
    return out
