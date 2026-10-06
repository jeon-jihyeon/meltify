"""Readable text of a saved web page, cited by heading anchor and line

`src.path` is the page URL, so a cite looks like `https://x.org/doc#install:28`, where
28 is the line in the extracted text and `install` is the id of the heading above it.
Pictures cite the same anchor plus their index on the page, as in `doc#install#img3`
"""

from __future__ import annotations

import base64
import binascii
import mimetypes
import re
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from urllib.parse import unquote, unquote_to_bytes, urlsplit

import lxml.etree
import lxml.html

from meltify.converters import Converted
from meltify.converters.embeds import OVER_TOTAL, Embeds
from meltify.converters.limits import MAX_PART_BYTES
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
XLINK_HREF = "{http://www.w3.org/1999/xlink}href"

# Image bytes for a picture reference, the reason it can't be read, or None when the
# loader listed that reason itself
Loaded = tuple[str, bytes] | str | None
Load = Callable[[str, Embeds], Loaded]


def _norm(text: str) -> str:
    return re.sub(r"\W+", "", text.replace("¶", "")).lower()


def _heading_id(h) -> str | None:
    hid = h.get("id") or next((e.get("id") for e in h.iterdescendants() if e.get("id")), None)
    parent = h.getparent()
    # Sphinx and many doc tools put the id on the section that wraps the heading,
    # which names only the first heading inside it
    wrapped = parent is not None and parent.tag in ("section", "div", "article")
    if not hid and wrapped and next((c for c in parent if c.tag in HEADS), None) is h:
        hid = parent.get("id")
    return hid


def _heading_ids(tree) -> list[tuple[str, str]]:
    """Normalized heading text and the nearest id, in document order"""
    return [(_norm(h.text_content()), hid) for h in tree.iter(*HEADS) if (hid := _heading_id(h))]


def data_uri(ref: str) -> Loaded:
    """Bytes of a `data:` URI, named by its media type so vector images are told apart"""
    head, comma, body = ref[5:].partition(",")
    if not comma:
        return "unreadable image"
    kind, *params = head.split(";")
    name = "inline" + (mimetypes.guess_extension(kind.strip().lower()) or "")
    try:
        if "base64" in params:
            data = base64.b64decode(re.sub(r"\s+", "", unquote(body)), validate=True)
        else:
            data = unquote_to_bytes(body)
    except (binascii.Error, ValueError):
        return "unreadable image"
    if not data:
        return "unreadable image"
    return "oversized image" if len(data) > MAX_PART_BYTES else (name, data)


def remote(ref: str, embeds: Embeds) -> Loaded:
    return "remote image"


def in_folder(page: Path, folder: Path) -> Load:
    """Loader for pictures beside a local page, refusing any path that leaves `folder`

    Each file is read once however many references reach it, under any spelling, and
    against the document's picture total
    """
    root = folder.resolve()
    read: dict[Path, Loaded] = {}

    def load(ref: str, embeds: Embeds) -> Loaded:
        parts = urlsplit(ref)
        if parts.scheme or parts.netloc:
            return "remote image"
        try:
            target = (page.parent / unquote(parts.path)).resolve()
            if not target.is_relative_to(root):
                return "out-of-folder image"
            if target not in read:
                read[target] = _local(target, root, embeds)
            return read[target]
        except (ValueError, OSError):
            # A null byte or an overlong name, and one bad reference shouldn't fail the page
            return "bad image reference"

    return load


def _local(target: Path, root: Path, embeds: Embeds) -> Loaded:
    if not target.is_file():
        return "missing image"
    if why := embeds.refusal(target.stat().st_size):
        return why
    data = embeds.hold(target.read_bytes())
    # The path inside the folder, so a/logo.svg and b/logo.svg stay apart
    return OVER_TOTAL if data is None else (target.relative_to(root).as_posix(), data)


def page_images(tree, src: Src, load: Load, embeds: Embeds) -> None:
    """Pictures in document order, each cited by its index and the heading id above it

    A page can show one big inline picture hundreds of times, so each distinct data URI is
    decoded once and every reference shares the held copy
    """
    inline: dict[str, Loaded] = {}
    anchor = None
    n = 0
    for el in tree.iter("img", "image", *HEADS):
        if el.tag in HEADS:
            anchor = _heading_id(el)
            continue
        # Lazy loading pages keep a placeholder in src and the real picture in data-src
        ref = el.get("data-src") or el.get("src") or el.get("href") or el.get(XLINK_HREF)
        # The HTML parser keeps an SVG image's namespaced attribute under its literal name
        ref = (ref or el.get("xlink:href") or "").strip()
        if not ref:
            continue
        n += 1
        if not ref.lower().startswith("data:"):
            got = load(ref, embeds)
        elif ref in inline:
            got = inline[ref]
        else:
            got = data_uri(ref)
            if isinstance(got, tuple):
                data = embeds.hold(got[1])
                got = OVER_TOTAL if data is None else (got[0], data)
            inline[ref] = got
        if isinstance(got, str):
            embeds.skipped[got] += 1
        elif got is not None:
            embeds.picture(replace(src, anchor=anchor, img=n), got[1], got[0])


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


def _whole(raw: bytes, url: str) -> str:
    # Its own tree, since the fallback below strips scripts in place
    tree = lxml.html.fromstring(raw)
    html = lxml.html.tostring(tree, encoding="unicode")
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


def convert_page(path: Path, src: Src, *, whole: bool = False, load: Load = remote) -> Converted:
    """Text and pictures of a page, where `load` fetches each picture it references

    By default the page came from the web, so every non-inline picture is remote
    """
    raw = path.read_bytes()
    out = Converted("web")
    try:
        tree = lxml.html.fromstring(raw)
    except (lxml.etree.ParserError, ValueError):
        out.needs.append("empty page")
        return out
    if whole:
        text = _whole(raw, src.path)
        extracted = len(re.sub(r"\s+", "", text))
    else:
        import trafilatura

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
        if not text:
            # trafilatura gives up on a page too short to look like an article. `extracted`
            # stays 0, so a script-built shell still asks for a render
            text = _whole(raw, src.path)
        elif title and not (first and first[0].startswith("#")):
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
    embeds = Embeds()
    page_images(tree, src, load, embeds)
    embeds.into(out)
    return out


def convert(path: Path, src: Src) -> Converted:
    # A saved page keeps navigation and footers, and its pictures sit next to it
    return convert_page(path, src, whole=True, load=in_folder(path, path.parent))
