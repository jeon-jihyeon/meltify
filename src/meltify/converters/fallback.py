"""Last resort for binary files no converter reads: render them, then read the pixels

Rendering loses structure and OCR guesses at text the file already holds, so files only
land here from the unknown entry or from a native converter whose parser gave up. The order
is LibreOffice, a full Quick Look preview, the text Spotlight indexes, and Quick Look's
first-page thumbnail
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from meltify import ffmpeg, safe
from meltify.converters import Block, Converted, RecognizeJob, recording, run
from meltify.evidence import Src
from meltify.needs import LIBREOFFICE, error_note

NEED = "unsupported format"
# Formats with a native converter. read hands a file here when that converter's parser gives up
NATIVE = {
    ".doc", ".dot", ".xls", ".xlt", ".xlsb", ".ppt", ".pps", ".pot", ".hwp",
    ".key", ".pages", ".numbers",
}  # fmt: skip
# Binary formats LibreOffice imports. Anything else would open in Writer as plain text and
# render its bytes as garbage
SOFFICE = NATIVE | {
    ".wpd", ".wps", ".wri", ".lwp", ".602", ".cwk", ".mcw", ".sdw", ".sxw", ".stw", ".dbf",
    ".wk1", ".wks", ".123", ".wb2", ".wq1", ".qpw", ".sdc", ".sxc", ".sdd", ".sxi", ".sxd",
    ".vsd", ".vss", ".vst", ".pub", ".cdr", ".cmx", ".wpg", ".pmd", ".p65", ".pm6", ".qxp",
    ".emf", ".wmf", ".cgm", ".met", ".pct", ".pict",
}  # fmt: skip
# Formats textutil reads. It treats anything else as plain text
TEXTUTIL = {".doc", ".dot", ".docx", ".rtf", ".odt", ".wordml", ".webarchive"}
SPOTLIGHT_TIMEOUT = 30
PREVIEW_SIDE = 2000
# Share of control and replacement characters real text can carry before it reads as
# a binary format's bytes
MAX_NOISE = 0.01
# Bytes read from each end of a file to tell what it is
HEAD = 4096
PROGRAMS = (
    b"\x7fELF",
    *(b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe"),
    # Java classes, and Mach-O universal binaries, which share the magic
    b"\xca\xfe\xba\xbe",
)
MEDIA_MAGIC = (
    b"RIFF", b"RF64", b"\x1aE\xdf\xa3", b"OggS", b"fLaC", b"ID3", b"FORM", b"caff",
    b".snd", b"#!AMR", b"MAC ", b"wvpk", b"TTA1", b"DSD ", b"FRM8", b"FLV", b".RMF",
    b"Creative Voice File", b"\x0bw", b"\x7f\xfe\x80\x01",
    # ASF, which WMA and WMV use
    b"\x30\x26\xb2\x75\x8e\x66\xcf\x11",
    # MPEG program streams and elementary video
    b"\x00\x00\x01\xba", b"\x00\x00\x01\xb3",
)  # fmt: skip
# Top-level boxes of MP4, MOV, M4A and 3GP, the first one at byte 4
ISO_BOXES = {b"ftyp", b"moov", b"mdat", b"free", b"wide", b"skip", b"pnot"}


def _picture(path: Path) -> bool:
    """Pillow decodes it, so the raster converter can read it like any listed image"""
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(path) as im:
            im.load()
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        EOFError,
        SyntaxError,
        Image.DecompressionBombError,
    ):
        # Pillow's plugins report a file they can't decode in any of these
        return False
    return True


def _thumbnail(path: Path, src: Src) -> Converted | None:
    """First page from `qlmanage -t`, for files whose full preview failed"""
    from meltify import imaging
    from meltify.converters import quicklook

    if not quicklook.available() or not quicklook.previewable(path):
        return None
    with tempfile.TemporaryDirectory(prefix="meltify-preview-") as tmp:
        args = ["qlmanage", "-t", "-s", str(PREVIEW_SIDE), "-o", tmp, str(path.resolve())]
        found = Path(tmp) / f"{path.name}.png"
        ok = quicklook.launch(args, quicklook.PREVIEW_TIMEOUT) == 0 and found.is_file()
        data = found.read_bytes() if ok else None
    size = None if data is None else imaging.image_size(data)
    if size is None or min(size) < imaging.MIN_SIDE:
        return None
    # Boxes cite as `a.xyz@px(...)` in the preview image, which shows only the first page
    return Converted(
        "rendered",
        needs=["first page only, read from a Quick Look preview"],
        jobs=[RecognizeJob("image", src, data=data)],
    )


def _unescape(raw: str) -> str:
    """A quoted value from mdimport's dump, which escapes UTF-16 units as `\\Uxxxx`"""
    units = bytearray()
    i = 0
    while i < len(raw):
        c = raw[i]
        if c == "\\" and i + 1 < len(raw):
            nxt = raw[i + 1]
            if nxt == "U" and re.fullmatch(r"[0-9a-fA-F]{4}", raw[i + 2 : i + 6]):
                units += int(raw[i + 2 : i + 6], 16).to_bytes(2, "little")
                i += 6
                continue
            c = {"n": "\n", "t": "\t", "r": "\r"}.get(nxt, nxt)
            i += 1
        units += c.encode("utf-16-le", "surrogatepass")
        i += 1
    return units.decode("utf-16-le", "replace")


def _readable(text: str) -> bool:
    # Importers that don't know a format hand back its bytes as text
    bad = sum(1 for c in text if (ord(c) < 32 and c not in "\t\n\r\f") or 0x7F <= ord(c) < 0xA0)
    return bool(text.strip()) and bad + text.count("\ufffd") <= len(text) * MAX_NOISE


def _spotlight_text(path: Path) -> str | None:
    """The text macOS indexes for the file, from textutil or a Spotlight importer"""
    if path.suffix.lower() in TEXTUTIL and shutil.which("textutil"):
        try:
            proc = safe.run(
                ["textutil", "-convert", "txt", "-stdout", str(path.resolve())],
                timeout=SPOTLIGHT_TIMEOUT, check=False,
            )  # fmt: skip
            if proc.returncode == 0 and _readable(proc.stdout):
                return proc.stdout
        except (OSError, subprocess.TimeoutExpired):
            pass
    from meltify.converters import quicklook

    # An importer only reads types macOS counts as content, and asking costs a process
    if shutil.which("mdimport") is None or not quicklook.previewable(path):
        return None
    try:
        # -t tests the importer without touching the index, -d3 dumps the attributes
        proc = safe.run(
            ["mdimport", "-t", "-d3", str(path.resolve())], timeout=SPOTLIGHT_TIMEOUT, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    found = re.search(r'kMDItemTextContent = "((?:[^"\\]|\\.)*)"', proc.stdout + proc.stderr)
    text = None if found is None else _unescape(found.group(1))
    return text if text and _readable(text) else None


def _spotlight(path: Path, src: Src) -> Converted | None:
    from meltify.converters import quicklook

    # Same switch as Quick Look, since both are macOS's own readers
    if not quicklook.available():
        return None
    text = _spotlight_text(path)
    if text is None:
        return None
    return Converted(
        "text",
        blocks=[Block(src, text.strip())],
        needs=["text only, read by macOS Spotlight without layout or pictures"],
    )


def _ends(path: Path) -> tuple[bytes, bytes]:
    with path.open("rb") as f:
        head = f.read(HEAD)
        size = f.seek(0, 2)
        f.seek(max(len(head), size - HEAD))
        return head, f.read()


def _program(head: bytes) -> bool:
    """Compiled code: ELF, Mach-O, Windows PE, Java classes and Python bytecode"""
    if head.startswith(PROGRAMS):
        return True
    if head.startswith(b"MZ") and len(head) >= 0x40:
        at = int.from_bytes(head[0x3C:0x40], "little")
        return head[at : at + 4] == b"PE\0\0"
    # A pyc starts with a 3.x magic number, CRLF, and a flags word of at most 3
    magic = int.from_bytes(head[:2], "little")
    flags = int.from_bytes(head[4:8], "little")
    return 3000 <= magic < 4000 and head[2:4] == b"\r\n" and len(head) >= 16 and flags <= 3


def _filler(head: bytes, tail: bytes) -> bool:
    """One byte repeated at both ends, like a zeroed disk image or a sparse placeholder"""
    return len(set(head + tail)) <= 1


def _media_like(head: bytes) -> bool:
    """Magic numbers of the containers and streams ffprobe could find timed media in"""
    if head.startswith(MEDIA_MAGIC) or head[4:8] in ISO_BOXES:
        return True
    # MPEG audio and ADTS AAC frames start with an 11-bit sync word
    if len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0:
        return True
    # MPEG transport streams repeat their sync byte every 188 bytes
    return len(head) > 188 and head[0] == head[188] == 0x47


def convert(path: Path, src: Src, need: str = NEED) -> Converted:
    from meltify.converters import render

    suffix = path.suffix.lower()
    context = run.current()
    if not context.fallback or context.shallow:
        return Converted("unknown", needs=[need])
    try:
        head, tail = _ends(path)
    except OSError:
        head, tail = b"", b""
    # No renderer or recognizer reads these, so they cost no process at all
    if path.is_file() and (_program(head) or _filler(head, tail)):
        return Converted("unknown", needs=[need])
    failed: list[str] = []
    if suffix in SOFFICE:
        try:
            # LibreOffice, then Quick Look when that misses
            out = render.read_rendered(
                path, src, lambda p, d: render.to_pdf(p, out_dir=d), "rendered"
            )
        except (OSError, RuntimeError, subprocess.SubprocessError) as e:
            # A crashed or hung soffice leaves the cheaper readers below still worth a try
            out = None
            failed.append(f"LibreOffice could not render it ({error_note(e)})")
        if out is not None:
            return out
    if _picture(path):
        from meltify.converters import raster

        out = raster.convert(path, src)
    elif _media_like(head) and (kind := ffmpeg.media_kind(path)) is not None:
        # The same path as a recording read by name, so subtitles and --subs-only apply
        out = recording(kind, path, src)
    else:
        out = None
        if suffix not in SOFFICE:
            out = render.read_rendered(path, src, render.preview, "rendered")
        out = out or _spotlight(path, src) or _thumbnail(path, src)
    if out is None:
        missing = suffix in SOFFICE and not failed and render.soffice() is None
        out = Converted("unknown", needs=[f"{need} ({LIBREOFFICE})" if missing else need])
    out.needs += failed
    return out
