"""Outlook .msg mail: markitdown's headers and body, with attachments melted as children

Pictures the body shows through a content id become OCR jobs cited as `mail.msg#img1`,
and every other attachment is melted again as `mail.msg#att=name`
"""

from __future__ import annotations

import codecs
import importlib.util
import struct
from dataclasses import replace
from pathlib import Path, PurePosixPath

from meltify.converters import IMAGES, Block, Child, Converted
from meltify.converters.embeds import VECTOR, Embeds
from meltify.converters.office import markdown_of
from meltify.evidence import Src
from meltify.needs import count
from meltify.safe import MissingTool

ATTACH = "__attach_version1.0_#"
DATA = "__substg1.0_37010102"
EMBEDDED = "__substg1.0_3701000D"
# Long filename, short filename, then display name, the order Outlook itself falls back in
NAMES = ("3707", "3704", "3001")
CONTENT_ID = "3712"
MIME = "370E"
SUBJECT, BODY = "0037", "1000"
PROPERTIES = "__properties_version1.0"
# PR_MESSAGE_CODEPAGE, then PR_INTERNET_CPID, as PT_LONG property tags
CODEPAGES = (0x3FFD0003, 0x3FDE0003)


def _bytes(ole, *path: str) -> bytes | None:
    joined = "/".join(path)
    return ole.openstream(joined).read() if ole.exists(joined) else None


def _codepage(ole, folder: str = "", header: int = 32) -> str | None:
    """Codec for the ANSI strings under `folder`, from its fixed-size properties

    The properties stream opens with a header of 32 bytes for the message and 24 for an
    embedded one, then 16-byte entries of tag, flags and value
    """
    raw = _bytes(ole, *filter(None, (folder, PROPERTIES))) or b""
    found = {}
    for at in range(header, len(raw) - 15, 16):
        tag, _, value = struct.unpack_from("<IIi", raw, at)
        if tag in CODEPAGES:
            found[tag] = value
    for tag in CODEPAGES:
        if (number := found.get(tag)) is None:
            continue
        try:
            return codecs.lookup(f"cp{number}").name
        except LookupError:
            continue
    return None


def _string(ole, folder: str, prop: str, encoding: str | None = None) -> str:
    # Unicode files store strings as UTF-16, older ANSI ones in the sender's code page
    if (raw := _bytes(ole, folder, f"__substg1.0_{prop}001F")) is not None:
        return raw.decode("utf-16-le", errors="replace").rstrip("\x00").strip()
    if (raw := _bytes(ole, folder, f"__substg1.0_{prop}001E")) is None:
        return ""
    raw = raw.rstrip(b"\x00")
    if encoding is not None:
        return raw.decode(encoding, errors="replace").strip()
    # No code page saved, so guess like archive names do, Korean Windows first
    for guess in ("utf-8", "cp949"):
        try:
            return raw.decode(guess).strip()
        except UnicodeDecodeError:
            continue
    return raw.decode("cp1252", errors="replace").strip()


def _is_image(name: str, mime: str) -> bool:
    suffix = PurePosixPath(name).suffix.lower()
    return mime.lower().startswith("image/") or suffix in IMAGES or suffix in VECTOR


def attachments(path: Path, src: Src, out: Converted) -> list[str]:
    """Add each attachment to `out` and return their names for the header"""
    try:
        import olefile
    except ImportError as e:
        raise MissingTool("olefile", "meltify doctor --install office") from e

    names: list[str] = []
    embeds = Embeds()
    shown = 0
    with olefile.OleFileIO(str(path)) as ole:
        encoding = _codepage(ole)
        entries = ole.listdir(streams=True, storages=True)
        folders = sorted({e[0] for e in entries if e[0].startswith(ATTACH)})
        for i, folder in enumerate(folders, start=1):
            name = next(
                (n for p in NAMES if (n := _string(ole, folder, p, encoding))), f"attachment-{i}"
            )
            # Senders' folders mean nothing in a cite, so keep only the file name
            name = PurePosixPath(name.replace("\\", "/")).name or f"attachment-{i}"
            names.append(name)
            data = _bytes(ole, folder, DATA)
            if ole.exists(f"{folder}/{EMBEDDED}"):
                # olefile can't write a nested message back out as a file, so its text is
                # read in place and its own attachments are listed as unread
                inner = f"{folder}/{EMBEDDED}"
                own = _codepage(ole, inner, header=24) or encoding
                text = "\n\n".join(t for p in (SUBJECT, BODY) if (t := _string(ole, inner, p, own)))
                if text:
                    out.blocks.append(Block(src.inside(name), text))
                else:
                    out.needs.append(f"empty embedded message {name}")
                inside = {e[2] for e in entries if e[:2] == [folder, EMBEDDED] and len(e) > 2}
                if n := sum(e.startswith(ATTACH) for e in inside):
                    out.needs.append(f"{count(n, 'attachment')} in {name} not read")
            elif not data:
                out.needs.append(f"empty or unreadable attachment {name}")
            elif _string(ole, folder, CONTENT_ID) and _is_image(
                name, _string(ole, folder, MIME, encoding)
            ):
                # A content id is how the body shows a picture in place, so it reads with the body
                shown += 1
                embeds.picture(replace(src, img=shown), data, name)
            else:
                out.children.append(Child(name, src, data))
    embeds.into(out)
    return names


def convert(path: Path, src: Src) -> Converted:
    out = Converted("mail")
    names = attachments(path, src, out)
    if importlib.util.find_spec("markitdown") is None:
        out.needs.append("markitdown")
        text = ""
    else:
        text = markdown_of("msg", path).strip()
    if names:
        text += "\n\nAttachments: " + ", ".join(names)
    if text.strip():
        # The body comes first, ahead of any forwarded message read in place
        out.blocks.insert(0, Block(src, text.strip()))
    return out
