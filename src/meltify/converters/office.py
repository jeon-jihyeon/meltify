from __future__ import annotations

import importlib.util
import posixpath
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path

from meltify.converters import Block, Converted, RecognizeJob
from meltify.evidence import Src
from meltify.safe import MissingTool

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "xdr": "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing",
    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
    "v": "urn:schemas-microsoft-com:vml",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}
R_EMBED = f"{{{NS['r']}}}embed"
R_LINK = f"{{{NS['r']}}}link"
R_ID = f"{{{NS['r']}}}id"
BLIP = f"{{{NS['a']}}}blip"
VML_IMAGE = f"{{{NS['v']}}}imagedata"
CHART = f"{{{NS['c']}}}chart"
PARA = f"{{{NS['w']}}}p"

# Pillow can't draw these, so they stay listed as needs instead of failing the file
VECTOR = {".emf", ".wmf", ".emz", ".wmz", ".svg"}
# A bigger member is more likely a zip bomb than a picture worth reading
MEMBER_MAX = 64 << 20


def xml(z: zipfile.ZipFile, part: str) -> ET.Element:
    data = z.read(part)
    # Entity declarations are the billion laughs payload, and real OOXML never has them
    if b"<!ENTITY" in data:
        raise ValueError(f"{part} declares XML entities")
    return ET.fromstring(data)


def rels(z: zipfile.ZipFile, part: str) -> dict[str, str | None]:
    """Relationship ids of a part mapped to member names, None for external links"""
    folder, name = posixpath.split(part)
    rel_part = f"{folder}/_rels/{name}.rels"
    if rel_part not in z.namelist():
        return {}
    out: dict[str, str | None] = {}
    for r in xml(z, rel_part).findall("rel:Relationship", NS):
        target = r.get("Target") or ""
        if r.get("TargetMode") == "External":
            out[r.get("Id") or ""] = None
        elif target.startswith("/"):
            # Some writers, openpyxl among them, store targets from the package root
            out[r.get("Id") or ""] = target.lstrip("/")
        else:
            out[r.get("Id") or ""] = posixpath.normpath(posixpath.join(folder, target))
    return out


@dataclass
class Embeds:
    """Images found inside a document, and the ones it can't read"""

    jobs: list[RecognizeJob] = field(default_factory=list)
    skipped: Counter[str] = field(default_factory=Counter)

    def add(self, src: Src, data: bytes, name: str = "") -> None:
        from meltify import imaging

        if posixpath.splitext(name)[1].lower() in VECTOR:
            self.skipped[f"{posixpath.splitext(name)[1][1:].lower()} image"] += 1
            return
        size = imaging.image_size(data)
        if size is None:
            self.skipped["unreadable image"] += 1
        elif min(size) >= imaging.MIN_SIDE:
            self.jobs.append(RecognizeJob("image", src, data=data))

    def member(
        self, z: zipfile.ZipFile, src: Src, targets: dict[str, str | None], rid: str | None
    ) -> None:
        if rid in targets and targets[rid] is None:
            self.skipped["linked image"] += 1
            return
        try:
            info = z.getinfo(targets.get(rid or "") or "")
        except KeyError:
            self.skipped["missing image"] += 1
            return
        if info.file_size > MEMBER_MAX:
            self.skipped["oversized image"] += 1
            return
        self.add(src, z.read(info), info.filename)

    def needs(self) -> list[str]:
        found = [(what, n) for what, n in self.skipped.items() if n]
        return [f"{n} {what}{'s' if n > 1 else ''} not read" for what, n in found]

    def into(self, out: Converted) -> None:
        out.jobs += self.jobs
        out.needs += self.needs()


def image_ids(el: ET.Element) -> list[str | None]:
    """Relationship ids of the pictures under an element, in document order"""
    found: list[str | None] = []
    for node in el.iter():
        if node.tag == BLIP:
            found.append(node.get(R_EMBED) or node.get(R_LINK))
        elif node.tag == VML_IMAGE:
            found.append(node.get(R_ID))
    return found


def docx_images(path: Path, src: Src) -> Embeds:
    embeds = Embeds()
    with zipfile.ZipFile(path) as z:
        part = "word/document.xml"
        targets = rels(z, part)
        count = 0
        para = 0

        def walk(el: ET.Element, at: int) -> None:
            nonlocal count, para
            if el.tag == PARA:
                para += 1
                at = para
            if el.tag == CHART:
                embeds.skipped["chart"] += 1
            elif el.tag in (BLIP, VML_IMAGE):
                count += 1
                rid = el.get(R_EMBED) or el.get(R_LINK) or el.get(R_ID)
                embeds.member(z, replace(src, para=at, img=count), targets, rid)
            for child in el:
                walk(child, at)

        walk(xml(z, part), 0)
    return embeds


def pptx_images(path: Path, src: Src) -> Embeds:
    embeds = Embeds()
    with zipfile.ZipFile(path) as z:
        part = "ppt/presentation.xml"
        targets = rels(z, part)
        slides = xml(z, part).find("p:sldIdLst", NS)
        for n, sld in enumerate([] if slides is None else slides, start=1):
            slide = targets.get(sld.get(R_ID) or "")
            if slide is None:
                continue
            root = xml(z, slide)
            if charts := sum(1 for _ in root.iter(CHART)):
                embeds.skipped["chart"] += charts
            pics = rels(z, slide)
            for i, rid in enumerate(image_ids(root), start=1):
                embeds.member(z, replace(src, slide=n, img=i), pics, rid)
    return embeds


def convert(path: Path, src: Src) -> Converted:
    if importlib.util.find_spec("markitdown") is None:
        raise MissingTool("markitdown", "meltify doctor --install office")
    from markitdown import MarkItDown

    text = MarkItDown().convert(str(path)).markdown
    out = Converted("office", [Block(src, text)] if text.strip() else [])
    suffix = path.suffix.lower()
    pictures = {".docx": docx_images, ".pptx": pptx_images}.get(suffix)
    if pictures is not None and zipfile.is_zipfile(path):
        pictures(path, src).into(out)
    return out
