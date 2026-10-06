"""Safari web archives: the saved page through the web extractor, its images through OCR

A webarchive is a property list holding the page bytes, every subresource the page
loaded, and one nested archive per frame
"""

from __future__ import annotations

import hashlib
import mimetypes
import plistlib
import posixpath
import tempfile
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

from meltify.converters import Child, Converted
from meltify.converters.embeds import Embeds
from meltify.evidence import Src
from meltify.needs import not_read

HTML = {"text/html", "application/xhtml+xml"}
# Distinct frames melted per archive, the rest listed in needs
MAX_FRAMES = 100
# Frame bytes per byte of the archive. Frames are slices of the archive, so unless they
# share bytes they can't add up to more than it
FRAME_RATIO = 2


def _name(resource: dict, fallback: str) -> str:
    """A file name that keeps a suffix, so the registry can melt the resource as a child"""
    name = posixpath.basename(urlparse(resource.get("WebResourceURL") or "").path) or fallback
    if not Path(name).suffix:
        name += mimetypes.guess_extension(resource.get("WebResourceMIMEType") or "") or ""
    return name


def _image(resource: object) -> bytes | None:
    # Stylesheets, scripts and fonts carry no text worth citing
    if not isinstance(resource, dict):
        return None
    data = resource.get("WebResourceData")
    image = str(resource.get("WebResourceMIMEType")).startswith("image/")
    return data if image and isinstance(data, bytes) else None


def _utf8(data: bytes, encoding: str | None) -> bytes:
    # Safari records the encoding the page was served with, which may disagree with its meta
    # tag, and a UTF-8 byte order mark outranks the meta tag in the HTML parser
    if not encoding:
        return data
    try:
        text = data.decode(encoding, errors="replace")
    except LookupError:
        return data
    return b"\xef\xbb\xbf" + text.removeprefix("\ufeff").encode("utf-8")


def _page(resource: dict, src: Src, images: dict[str, tuple[str, bytes]], used: set[str]):
    """The page, with the pictures it references loaded from the archive"""
    from meltify.converters.web import convert_page

    base = resource.get("WebResourceURL") or ""

    def load(ref: str, embeds: Embeds) -> tuple[str, bytes] | str:
        url = urldefrag(urljoin(base, ref)).url
        if url not in images:
            return "remote image"
        used.add(url)
        return images[url]

    data = _utf8(resource["WebResourceData"], resource.get("WebResourceTextEncodingName"))
    with tempfile.TemporaryDirectory(prefix="meltify-webarchive-") as tmp:
        page = Path(tmp) / "page.html"
        page.write_bytes(data)
        # A saved page keeps navigation and footers, as a saved .html does
        out = convert_page(page, src, whole=True, load=load)
    out.kind = "webarchive"
    return out


def convert(path: Path, src: Src) -> Converted:
    raw = path.read_bytes()
    try:
        archive = plistlib.loads(raw)
    except ValueError as e:
        return Converted("webarchive", needs=[f"unreadable webarchive: {e}"[:200]])
    main = archive.get("WebMainResource") if isinstance(archive, dict) else None
    if not isinstance(main, dict) or not isinstance(main.get("WebResourceData"), bytes):
        return Converted("webarchive", needs=["webarchive has no main resource"])

    embeds = Embeds()
    images: dict[str, tuple[str, bytes]] = {}
    for i, res in enumerate(archive.get("WebSubresources") or [], start=1):
        data = _image(res)
        if data is None:
            continue
        # Every picture is already in memory with the plist, so only the size limit applies
        # here, and the page's references share each one under the picture total
        if why := embeds.refusal(len(data)):
            embeds.skipped[why] += 1
            continue
        images[urldefrag(res.get("WebResourceURL") or f"#{i}").url] = (
            _name(res, f"image{i}"),
            data,
        )

    used: set[str] = set()
    if (main.get("WebResourceMIMEType") or "text/html") in HTML:
        out = _page(main, src, images, used)
    else:
        # Safari also saves a bare image or PDF this way, so it melts like the file itself
        out = Converted("webarchive")
        out.children.append(Child(_name(main, "main"), src, data=main["WebResourceData"]))
    # Pictures only a stylesheet or script loads, cited by their name in the archive
    for url, (name, data) in images.items():
        if url not in used:
            # An SVG melts as a child under its own name, so it hangs off the archive itself
            at = src if Path(name).suffix.lower() == ".svg" else src.inside(name)
            embeds.picture(at, data, name)
    embeds.into(out)

    out.needs += _frames(archive.get("WebSubframeArchives") or [], src, len(raw), out)
    return out


def _frames(frames: list, src: Src, size: int, out: Converted) -> list[str]:
    """Each frame is a whole webarchive of its own, melted again as a child

    A binary plist can reference one frame object many times, and dumping each reference
    again would multiply the archive at every nesting level. Identical frames go out once,
    and the frames together never get more bytes than the archive itself holds
    """
    seen: set[int] = set()
    dumped: set[bytes] = set()
    left = FRAME_RATIO * size
    many = large = 0
    for i, frame in enumerate(frames, start=1):
        if id(frame) in seen:
            continue
        seen.add(id(frame))
        if len(dumped) >= MAX_FRAMES:
            many += 1
            continue
        data = plistlib.dumps(frame, fmt=plistlib.FMT_BINARY)
        digest = hashlib.sha256(data).digest()
        if digest in dumped:
            continue
        if len(data) > left:
            large += 1
            continue
        left -= len(data)
        dumped.add(digest)
        out.children.append(Child(f"frame{i}.webarchive", src, data=data))
    needs = []
    if many:
        needs.append(not_read(many, "frame", f"over {MAX_FRAMES} frames"))
    if large:
        needs.append(not_read(large, "frame", "more frame data than the archive holds"))
    return needs
