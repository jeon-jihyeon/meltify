"""A mixed folder of documents like the ones handed over with a task"""

from __future__ import annotations

import io
from email.message import EmailMessage
from pathlib import Path


def calendar_xlsx() -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Calendar"
    ws.append(["date", "event"])
    ws.append(["2025-07-21", "handover meeting"])
    ws["B7"] = "budget review"
    secret = wb.create_sheet("Old")
    secret.sheet_state = "hidden"
    secret.append(["archived"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def mixed_folder(root: Path) -> Path:
    import pymupdf
    from PIL import Image

    root.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Page one says the deadline is Friday", fontsize=12)
    doc.new_page()
    doc.save(root / "report.pdf")

    (root / "calendar.xlsx").write_bytes(calendar_xlsx())
    (root / "notes.txt").write_text("first line\n둘째 줄 한글\nthird line\n", encoding="utf-8")
    (root / "legacy.txt").write_bytes("완료 보고".encode("cp949"))
    Image.new("RGB", (40, 20), "white").save(root / "scan.png")
    (root / "blob.bin").write_bytes(b"\x00\x01\x02binary")

    msg = EmailMessage()
    msg["From"] = "lead@example.com"
    msg["To"] = "new@example.com"
    msg["Subject"] = "handover"
    msg.set_content("Please check the attached calendar.")
    msg.add_attachment(
        calendar_xlsx(),
        maintype="application",
        subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="cal.xlsx",
    )
    (root / "mail").mkdir()
    (root / "mail" / "handover.eml").write_bytes(msg.as_bytes())
    (root / ".hidden.txt").write_text("skip me")
    return root


def nested_mail(path: Path, levels: int) -> Path:
    """A mail forwarded inside itself `levels` times, with a text attachment at the bottom"""
    msg = EmailMessage()
    msg["Subject"] = "level 0"
    msg.set_content("innermost body")
    msg.add_attachment(b"deep note", maintype="text", subtype="plain", filename="deep.txt")
    for n in range(1, levels + 1):
        outer = EmailMessage()
        outer["Subject"] = f"level {n}"
        outer.set_content(f"forward {n}")
        outer.add_attachment(msg)
        msg = outer
    path.write_bytes(msg.as_bytes())
    return path


def tricky_mail(path: Path, absolute: Path) -> Path:
    """Attachment names that escape the output folder or repeat inside one mail"""
    msg = EmailMessage()
    msg["Subject"] = "tricky"
    msg.set_content("see attachments")
    for name, body in [
        ("../../../../escape.txt", b"escape"),
        (str(absolute), b"absolute"),
        ("..", b"dots"),
        ("dup.txt", b"first copy"),
        ("dup.txt", b"second copy"),
    ]:
        msg.add_attachment(body, maintype="text", subtype="plain", filename=name)
    path.write_bytes(msg.as_bytes())
    return path
