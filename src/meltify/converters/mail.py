from __future__ import annotations

import email
import mimetypes
import re
from collections.abc import Iterator
from dataclasses import replace
from email import policy
from email.message import EmailMessage
from pathlib import Path, PurePosixPath

from meltify.converters import Block, Child, Converted
from meltify.evidence import Src

HEADERS = ("From", "To", "Cc", "Date", "Subject")


def _html_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _leaves(part: EmailMessage) -> Iterator[EmailMessage]:
    # Stop at forwarded messages, since their images belong to their own child row
    if part.get_content_maintype() == "multipart":
        for sub in part.iter_parts():
            yield from _leaves(sub)
    else:
        yield part


def _inline_images(msg: EmailMessage, skip: list[EmailMessage]) -> list[tuple[str, bytes]]:
    """Images shown in the body, like multipart/related ones, which iter_attachments skips"""
    from meltify import imaging

    found = []
    for part in _leaves(msg):
        if part.get_content_maintype() != "image" or any(part is s for s in skip):
            continue
        data = part.get_payload(decode=True)
        size = imaging.image_size(data) if data else None
        # Tracking pixels and signature icons carry no text
        if size is None or min(size) < imaging.MIN_SIDE:
            continue
        ext = mimetypes.guess_extension(part.get_content_type()) or ".img"
        found.append((part.get_filename() or f"inline-{len(found) + 1}{ext}", data))
    return found


def convert(path: Path, src: Src) -> Converted:
    msg = email.message_from_bytes(path.read_bytes(), policy=policy.default)
    out = Converted("mail")
    head = "\n".join(f"{h}: {msg[h]}" for h in HEADERS if msg[h])
    body_part = msg.get_body(("plain", "html"))
    body = ""
    if body_part is not None:
        body = body_part.get_content()
        if body_part.get_content_type() == "text/html":
            body = _html_text(body)
    names = []
    attached = list(msg.iter_attachments())
    for i, att in enumerate(attached):
        name = att.get_filename() or f"attachment-{i + 1}"
        if att.get_content_type() == "message/rfc822":
            # A forwarded mail has no decodable payload, so save its message as .eml
            data = att.get_content().as_bytes()
            name = name if name.lower().endswith(".eml") else f"{name}.eml"
        else:
            data = att.get_payload(decode=True)
        names.append(name)
        if data:
            # Mail attachments are flat, so a sender's directories mean nothing in a cite
            out.children.append(Child(PurePosixPath(name.replace("\\", "/")).name, src, data))
        else:
            out.needs.append(f"empty or unreadable attachment {name}")
    if names:
        head += "\nAttachments: " + ", ".join(names)
    inline = _inline_images(msg, attached)
    for name, data in inline:
        out.children.append(Child(PurePosixPath(name.replace("\\", "/")).name, src, data))
    if inline:
        head += "\nInline images: " + ", ".join(n for n, _ in inline)
    out.blocks.append(Block(replace(src), f"{head}\n\n{body.strip()}"))
    return out
