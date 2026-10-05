from __future__ import annotations

import email
import re
from dataclasses import replace
from email import policy
from pathlib import Path

from meltify.converters import Block, Converted
from meltify.evidence import Src

HEADERS = ("From", "To", "Cc", "Date", "Subject")


def _html_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


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
    for i, att in enumerate(msg.iter_attachments()):
        name = att.get_filename() or f"attachment-{i + 1}"
        if att.get_content_type() == "message/rfc822":
            # A forwarded mail has no decodable payload, so save its message as .eml
            data = att.get_content().as_bytes()
            name = name if name.lower().endswith(".eml") else f"{name}.eml"
        else:
            data = att.get_payload(decode=True)
        names.append(name)
        if data:
            out.attachments.append((name, data))
        else:
            out.needs.append(f"empty or unreadable attachment {name}")
    if names:
        head += "\nAttachments: " + ", ".join(names)
    out.blocks.append(Block(replace(src), f"{head}\n\n{body.strip()}"))
    return out
