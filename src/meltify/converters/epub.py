"""Book text from markitdown, plus the pictures each spine document shows

A picture cites the spine position as its section, the heading id above it and its index
in that document, as in `book.epub#ch2#s3#img1`
"""

from __future__ import annotations

import importlib.util
import posixpath
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlsplit

from meltify.converters import Block, Converted
from meltify.converters.embeds import Embeds
from meltify.converters.limits import MAX_PART_BYTES, human_bytes, inflates
from meltify.converters.office import markdown_of
from meltify.converters.xmlsafe import zip_xml
from meltify.evidence import Src

if TYPE_CHECKING:
    from meltify.converters.web import Load, Loaded

OPF = "{http://www.idpf.org/2007/opf}"
CONTAINER = "{urn:oasis:names:tc:opendocument:xmlns:container}"
# markitdown reads every spine document into memory, so a bigger book is refused up front
MAX_TEXT_BYTES = 256 << 20


def spine(z: zipfile.ZipFile) -> list[str]:
    """Member names of the reading order, as the package document lists it"""
    rootfile = zip_xml(z, "META-INF/container.xml").find(f".//{CONTAINER}rootfile")
    opf = "" if rootfile is None else rootfile.get("full-path") or ""
    if z.getinfo(opf).file_size > MAX_PART_BYTES:
        raise ValueError(f"{opf} is over the {human_bytes(MAX_PART_BYTES)} limit")
    package = zip_xml(z, opf)
    base = posixpath.dirname(opf)
    hrefs = {i.get("id"): i.get("href") or "" for i in package.iter(f"{OPF}item")}
    return [
        posixpath.normpath(posixpath.join(base, unquote(urlsplit(hrefs[ref]).path)))
        for r in package.iter(f"{OPF}itemref")
        if (ref := r.get("idref")) in hrefs
    ]


def in_book(z: zipfile.ZipFile, doc: str) -> Load:
    """Loader for pictures a spine document points at, which only ever reads zip members

    Members go through the book's Embeds, so a picture shown in many places across the
    chapters is unpacked once and every picture counts against one total
    """
    folder = posixpath.dirname(doc)

    def load(ref: str, embeds: Embeds) -> Loaded:
        parts = urlsplit(ref)
        if parts.scheme or parts.netloc:
            return "remote image"
        name = posixpath.normpath(posixpath.join(folder, unquote(parts.path)))
        data = embeds.load(z, name)
        return None if data is None else (name, data)

    return load


def pictures(z: zipfile.ZipFile, docs: list[str], src: Src) -> Embeds:
    import lxml.etree
    import lxml.html

    from meltify.converters.web import page_images

    embeds = Embeds()
    for n, doc in enumerate(docs, start=1):
        try:
            tree = lxml.html.fromstring(z.read(doc))
        except KeyError:
            embeds.skipped["missing chapter"] += 1
            continue
        except (lxml.etree.ParserError, ValueError):
            # An empty document has nothing to show, pictures included
            continue
        page_images(tree, replace(src, section=n), in_book(z, doc), embeds)
    return embeds


def convert(path: Path, src: Src) -> Converted:
    with zipfile.ZipFile(path) as z:
        docs = spine(z)
        names = set(z.namelist())
        infos = [z.getinfo(d) for d in docs if d in names]
        sizes = [i.file_size for i in infos]
        if max(sizes, default=0) > MAX_PART_BYTES or sum(sizes) > MAX_TEXT_BYTES:
            raise ValueError(f"spine documents are over the {human_bytes(MAX_TEXT_BYTES)} limit")
        if bomb := next((i for i in infos if inflates(i.file_size, i.compress_size)), None):
            raise ValueError(f"{bomb.filename} expands like a zip bomb")
        embeds = pictures(z, docs, src)
    if importlib.util.find_spec("markitdown") is None:
        # Pictures come straight from the zip, so they're still worth reading without the text
        out = Converted("epub", needs=["markitdown"])
    else:
        text = markdown_of("epub", path)
        out = Converted("epub", [Block(src, text)] if text.strip() else [])
    embeds.into(out)
    return out
