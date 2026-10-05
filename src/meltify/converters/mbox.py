from __future__ import annotations

import mailbox
from email import policy
from email.parser import BytesHeaderParser
from pathlib import Path

from meltify.converters import Block, Child, Converted
from meltify.evidence import Src

HEADERS = ("Date", "From", "Subject")


def convert(path: Path, src: Src) -> Converted:
    """List the messages and hand each one to the mail converter as `msg-N.eml`"""
    box = mailbox.mbox(path, create=False)
    out = Converted("mbox")
    lines = []
    parser = BytesHeaderParser(policy=policy.default)
    try:
        for i, key in enumerate(box.iterkeys(), start=1):
            data = box.get_bytes(key)
            head = parser.parsebytes(data)
            name = f"msg-{i}.eml"
            fields = " | ".join(str(head[h] or "") for h in HEADERS)
            lines.append(f"{name} | {fields}")
            out.children.append(Child(name, src, data=data))
    finally:
        box.close()
    if lines:
        out.blocks.append(Block(src, "message | " + " | ".join(HEADERS) + "\n" + "\n".join(lines)))
    else:
        out.needs.append("no messages found")
    return out
