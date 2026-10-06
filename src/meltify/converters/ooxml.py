"""OOXML packages: what a zip holds by its content types, and where its objects sit"""

from __future__ import annotations

import functools
import posixpath
import shutil
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from meltify.converters.xmlsafe import zip_xml

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "xdr": "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing",
    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
    "dgm": "http://schemas.openxmlformats.org/drawingml/2006/diagram",
    "asvg": "http://schemas.microsoft.com/office/drawing/2016/SVG/main",
    "v": "urn:schemas-microsoft-com:vml",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
}
R_EMBED = f"{{{NS['r']}}}embed"
R_LINK = f"{{{NS['r']}}}link"
R_ID = f"{{{NS['r']}}}id"
R_DM = f"{{{NS['r']}}}dm"
BLIP = f"{{{NS['a']}}}blip"
SVG_BLIP = f"{{{NS['asvg']}}}svgBlip"
VML_IMAGE = f"{{{NS['v']}}}imagedata"
CHART = f"{{{NS['c']}}}chart"
DIAGRAM = f"{{{NS['dgm']}}}relIds"
PARA = f"{{{NS['w']}}}p"
TEXT = f"{{{NS['a']}}}t"
ALTERNATE = f"{{{NS['mc']}}}AlternateContent"
CHOICE, FALLBACK = f"{{{NS['mc']}}}Choice", f"{{{NS['mc']}}}Fallback"
# What makes an mc:Choice worth reading over its Fallback
READABLE = {BLIP, VML_IMAGE, CHART, DIAGRAM, PARA, TEXT}

# Words in a main part content type that name the family, for the macro and template
# variants too, like ms-word.template.macroEnabledTemplate.main+xml
FAMILY = {
    "wordprocessingml": "docx",
    "ms-word": "docx",
    "presentationml": "pptx",
    "ms-powerpoint": "pptx",
    "spreadsheetml": "xlsx",
    "ms-excel": "xlsx",
}
XLSB_MAIN = "application/vnd.ms-excel.sheet.binary.macroEnabled.main"
# Hancom Office 2014 and later saves these as plain OOXML under its own names
HANCOM = {".show": ".pptx", ".cell": ".xlsx"}


def integer(value: str | None, default: int = 0) -> int:
    try:
        return int(value or default)
    except ValueError:
        return default


def hancom_need(suffix: str) -> str:
    """Why an older Hancom .show or .cell, which isn't OOXML yet, stays unread"""
    return f"Hancom {suffix} before 2014 not read (save it as {HANCOM[suffix]})"


def rels(z: zipfile.ZipFile, part: str) -> dict[str, str | None]:
    """Relationship ids of a part mapped to member names, None for external links"""
    folder, name = posixpath.split(part)
    rel_part = posixpath.join(folder, "_rels", f"{name}.rels")
    if rel_part not in z.namelist():
        return {}
    out: dict[str, str | None] = {}
    for r in zip_xml(z, rel_part).findall("rel:Relationship", NS):
        target = r.get("Target") or ""
        if r.get("TargetMode") == "External":
            out[r.get("Id") or ""] = None
        elif target.startswith("/"):
            # Some writers, openpyxl among them, store targets from the package root
            out[r.get("Id") or ""] = target.lstrip("/")
        else:
            out[r.get("Id") or ""] = posixpath.normpath(posixpath.join(folder, target))
    return out


@dataclass(frozen=True)
class Package:
    """What an OOXML zip holds, read from its content types instead of its name"""

    family: str  # docx, pptx, xlsx or xlsb
    main: str  # member name of the main part
    content_type: str


def _main_family(content_type: str) -> str | None:
    if content_type == XLSB_MAIN:
        # Its sheets are binary records that only calamine reads, not openpyxl
        return "xlsb"
    if not content_type.endswith(".main+xml"):
        return None
    return next((FAMILY[w] for w in content_type.split(".") if w in FAMILY), None)


def _content_type(root: ET.Element, part: str) -> str:
    """A part's type from its Override, else from the Default for its extension"""
    for override in root.iterfind("ct:Override", NS):
        if (override.get("PartName") or "").lstrip("/").lower() == part.lower():
            return override.get("ContentType") or ""
    ext = posixpath.splitext(part)[1][1:].lower()
    for default in root.iterfind("ct:Default", NS):
        if (default.get("Extension") or "").lower() == ext:
            return default.get("ContentType") or ""
    return ""


def _office_documents(z: zipfile.ZipFile) -> list[str]:
    try:
        root = zip_xml(z, "_rels/.rels")
    except KeyError:
        return []
    found = []
    for r in root.iterfind("rel:Relationship", NS):
        # Transitional and Strict OOXML name the relationship under different roots
        if (r.get("Type") or "").endswith("/officeDocument") and r.get("TargetMode") != "External":
            found.append(posixpath.normpath((r.get("Target") or "").lstrip("/")))
    return found


def package(z: zipfile.ZipFile) -> Package | None:
    try:
        root = zip_xml(z, "[Content_Types].xml")
    except KeyError:
        return None
    # The package relationship names the main part, whose type may come from a Default
    for target in _office_documents(z):
        content_type = _content_type(root, target)
        if kind := _main_family(content_type):
            return Package(kind, target, content_type)
    # Some writers skip the relationship, so any main part override still counts
    for override in root.iterfind("ct:Override", NS):
        content_type = override.get("ContentType") or ""
        if kind := _main_family(content_type):
            return Package(kind, (override.get("PartName") or "").lstrip("/"), content_type)
    return None


@functools.lru_cache(maxsize=64)
def _package_at(path: str, size: int, mtime: int) -> Package | None:
    # Size and mtime sit in the key, so a file rewritten in place is read again
    try:
        with zipfile.ZipFile(path) as z:
            return package(z)
    except (OSError, zipfile.BadZipFile, ET.ParseError, ValueError):
        return None


def package_of(path: Path) -> Package | None:
    """The package a file holds, None for anything that isn't a readable OOXML zip

    The sniff and then the converter both ask about the same file, so the answer is kept
    instead of unzipping it each time
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    return _package_at(str(path.absolute()), stat.st_size, stat.st_mtime_ns)


def family(path: Path) -> str | None:
    found = package_of(path)
    return found.family if found else None


def with_parts(path: Path, parts: dict[str, bytes], out: Path) -> None:
    """A copy of the package with some parts replaced, streamed so media never sit in memory"""
    with zipfile.ZipFile(path) as z, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in z.infolist():
            if info.filename in parts:
                dst.writestr(info, parts[info.filename])
                continue
            with (
                z.open(info) as r,
                dst.open(zipfile.ZipInfo(info.filename, info.date_time), "w") as w,
            ):
                shutil.copyfileobj(r, w)


def branch(alternate: ET.Element) -> ET.Element | None:
    """The branch of an mc:AlternateContent to read, the way Office picks one to show

    The first Choice holding anything meltify reads wins, and the Fallback, often the
    same text box or picture again in VML, only stands in when none does
    """
    for choice in alternate.iterfind(CHOICE):
        if any(el.tag in READABLE for el in choice.iter()):
            return choice
    return alternate.find(FALLBACK)


def shown(root: ET.Element) -> Iterator[tuple[ET.Element, list[ET.Element]]]:
    """Elements under root in document order with their ancestors, outermost first

    Only one branch of each mc:AlternateContent is walked, so nothing is read twice.
    The ancestor list is reused, so copy it to keep it. A stack instead of recursion
    keeps deep XML from blowing the call stack
    """
    stack: list[tuple[ET.Element, int]] = [(root, 0)]
    up: list[ET.Element] = []
    while stack:
        el, depth = stack.pop()
        del up[depth:]
        yield el, up
        up.append(el)
        kids = [branch(el)] if el.tag == ALTERNATE else list(el)
        stack += [(kid, depth + 1) for kid in reversed(kids) if kid is not None]


def objects(root: ET.Element) -> list[tuple[str, str | None, tuple[ET.Element, ...]]]:
    """Pictures, charts and diagrams under root in document order

    Each comes as its kind, relationship id and ancestors, outermost first
    """
    found: list[tuple[str, str | None, tuple[ET.Element, ...]]] = []
    for el, up in shown(root):
        if el.tag == BLIP:
            # Office keeps a PNG fallback in r:embed and the original SVG in an extension
            svg = el.find(f".//{SVG_BLIP}")
            rid = (el.get(R_EMBED) or el.get(R_LINK)) if svg is None else svg.get(R_EMBED)
            found.append(("picture", rid, tuple(up)))
        elif el.tag == VML_IMAGE:
            found.append(("picture", el.get(R_ID), tuple(up)))
        elif el.tag == CHART:
            found.append(("chart", el.get(R_ID), tuple(up)))
        elif el.tag == DIAGRAM:
            found.append(("diagram", el.get(R_DM), tuple(up)))
    return found


def paragraphs(root: ET.Element) -> dict[int, int]:
    """Each shown w:p's number in document order, keyed by id, from 1

    Paragraphs in table cells and text boxes count too, in the order they appear
    """
    found = (el for el, _ in shown(root) if el.tag == PARA)
    return {id(p): n for n, p in enumerate(found, start=1)}
