from __future__ import annotations

import base64
import io
import json
import mailbox
import sqlite3
import zipfile
from email.message import EmailMessage
from pathlib import Path

import pytest

from meltify.converters import pick
from meltify.evidence import Src

FIXTURES = Path(__file__).parent / "fixtures" / "sources"
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64).decode()


def melt(path: Path):
    kind, convert = pick(path)
    return kind, convert(path, Src(path.name))


def test_notebook_cells_cite_and_images_become_jobs(tmp_path: Path) -> None:
    nb = {
        "nbformat": 4,
        "metadata": {"kernelspec": {"language": "python"}},
        "cells": [
            {"cell_type": "markdown", "source": ["# Sales\n", "매출 분석"]},
            {
                "cell_type": "code",
                "source": "df.describe()",
                "outputs": [
                    {"output_type": "execute_result", "data": {"text/plain": "count 10"}},
                    {"output_type": "stream", "name": "stdout", "text": ["\x1b[31mred\x1b[0m\n"]},
                ],
            },
            {
                "cell_type": "code",
                "source": "plt.plot(x)",
                "outputs": [
                    {
                        "output_type": "display_data",
                        "data": {"image/png": PNG, "text/plain": "<Figure size 640x480>"},
                    }
                ],
            },
        ],
    }
    path = tmp_path / "a.ipynb"
    path.write_text(json.dumps(nb), encoding="utf-8")
    kind, out = melt(path)
    assert kind == "notebook"
    assert [b.src.cite() for b in out.blocks] == [
        "a.ipynb#cell=1",
        "a.ipynb#cell=2",
        "a.ipynb#cell=3",
    ]
    assert "```python\ndf.describe()\n```" in out.blocks[1].text
    assert "count 10" in out.blocks[1].text and "red" in out.blocks[1].text
    assert "\x1b" not in out.blocks[1].text
    assert PNG[:20] not in out.markdown("a.ipynb") and "<Figure" not in out.blocks[2].text
    assert [(j.kind, j.src.cite()) for j in out.jobs] == [("image", "a.ipynb#cell=3#img1")]
    assert out.jobs[0].data.startswith(b"\x89PNG")


def test_mbox_messages_become_mail_children(tmp_path: Path) -> None:
    path = tmp_path / "a.mbox"
    box = mailbox.mbox(path)
    for i in range(2):
        m = EmailMessage()
        m["From"], m["To"], m["Subject"] = "a@x.com", "b@y.com", f"회의 {i}"
        m.set_content(f"본문 {i}")
        box.add(m)
    box.flush()
    box.close()
    kind, out = melt(path)
    assert kind == "mbox"
    assert [c.name for c in out.children] == ["msg-1.eml", "msg-2.eml"]
    assert out.children[0].parent.inside("msg-1.eml").cite() == "a.mbox#att=msg-1.eml"
    assert "회의 1" in out.blocks[0].text
    # Each child goes back through the registry as a mail
    saved = tmp_path / "msg-2.eml"
    saved.write_bytes(out.children[1].data)
    child_kind, child = melt(saved)
    assert child_kind == "mail" and "본문 1" in child.blocks[0].text


def test_sqlite_schema_sample_rows_and_rowid_cites(tmp_path: Path) -> None:
    path = tmp_path / "a.db"
    db = sqlite3.connect(path)
    db.execute("create table users(id integer primary key, name text, avatar blob)")
    db.executemany(
        "insert into users(name, avatar) values(?, ?)", [(f"u{i}", b"\x00" * 9) for i in range(50)]
    )
    db.execute("create table tags(k text primary key, v text) without rowid")
    db.execute("insert into tags values('a', 'b|c')")
    db.commit()
    db.close()
    before = path.stat().st_mtime_ns
    kind, out = melt(path)
    assert kind == "sqlite"
    assert "CREATE TABLE users" in out.blocks[0].text
    cites = [b.src.cite() for b in out.blocks]
    assert cites == ["a.db", "a.db#users!rowid=1", "a.db#tags"]
    users = out.blocks[1].text.splitlines()
    assert users[0] == "users: first 20 of 50 rows"
    assert users[2] == "rowid=1| 1 | u0 | <blob 9 bytes>"
    assert len(users) == 22
    assert "row 1| a | b\\|c" in out.blocks[2].text
    assert path.stat().st_mtime_ns == before
    assert not (tmp_path / "a.db-journal").exists()


def test_sqlite_garbage_is_a_need_not_a_crash(tmp_path: Path) -> None:
    path = tmp_path / "Thumbs.db"
    path.write_bytes(b"not a database at all" * 10)
    _, out = melt(path)
    assert out.needs and "unreadable database" in out.needs[0]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (
            "kakao_windows.txt",
            [
                (5, "[2026-10-05 20:36 테스트] ㅎㅇ"),
                (6, "[2026-10-05 20:38 나] 내일 3시"),
                (7, "여러 줄 이어짐"),
                (9, "[2026-10-06 00:05 테스트] 자정 넘음"),
            ],
        ),
        (
            "kakao_android.txt",
            [
                (5, "[2021-12-29 20:36 홍길동] 안녕하세요"),
                (6, "[2021-12-29 20:37 김철수] 네 반갑습니다"),
                (7, "여러 줄 메시지"),
                (8, "[2021-12-29 20:38] 이영희님이 들어왔습니다."),
            ],
        ),
        (
            "kakao_ios.txt",
            [
                (5, "[2014-12-31 19:12 Alice] Happy new year"),
                (6, "[2014-12-31 19:13 Bob] You too"),
                (7, "[2014-12-31 19:14 Alice] See you at 9 : 30"),
            ],
        ),
        (
            "kakao_mac.csv",
            [
                (2, "[2021-12-29 20:36 홍길동] 안녕하세요"),
                (3, "[2021-12-29 20:37 김철수] 여러 줄 메시지"),
                (5, "[2021-12-29 20:39 홍길동] 좋아요, 내일 봐요"),
            ],
        ),
    ],
)
def test_kakao_exports(name: str, expected: list[tuple[int, str]]) -> None:
    kind, out = melt(FIXTURES / name)
    assert kind == "kakao"
    rows = {}
    for line in "\n".join(b.text for b in out.blocks).splitlines():
        n, _, text = line.partition("| ")
        rows[int(n)] = text
    for n, text in expected:
        assert rows[n] == text
    assert out.blocks[0].src.cite().startswith(f"{name}:")


def test_plain_txt_and_csv_are_not_kakao(tmp_path: Path) -> None:
    note = tmp_path / "notes.txt"
    note.write_text("[todo] [later] tidy up\nbuy milk\n", encoding="utf-8")
    table = tmp_path / "t.csv"
    table.write_text("Date,User,Amount\n2021-01-01,a,3\n", encoding="utf-8")
    assert pick(note)[0] == "text"
    assert pick(table)[0] == "text"


def _slack_zip(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(
            "users.json",
            json.dumps(
                [
                    {"id": "U1", "name": "kim", "profile": {"display_name": "김"}},
                    {"id": "U2", "name": "lee", "real_name": "Lee"},
                ]
            ),
        )
        z.writestr("channels.json", json.dumps([{"id": "C1", "name": "general"}]))
        z.writestr(
            "general/2026-10-05.json",
            json.dumps(
                [
                    {"ts": "1791158400.000100", "user": "U1", "text": "hi <@U2> see <#C1>"},
                    {
                        "ts": "1791158460.000200",
                        "user": "U2",
                        "text": "<https://x.org|the doc> &amp; more",
                        "thread_ts": "1791158400.000100",
                        "files": [{"name": "plan.pdf"}],
                    },
                ]
            ),
        )


def test_slack_export_names_and_ts_cites(tmp_path: Path) -> None:
    path = tmp_path / "export.zip"
    _slack_zip(path)
    kind, out = melt(path)
    assert kind == "slack"
    assert out.blocks[0].src.cite() == "export.zip#att=general/2026-10-05.json#ts=1791158400.000100"
    lines = out.blocks[0].text.splitlines()
    assert lines[0] == "1791158400.000100| [2026-10-05 00:00 UTC 김] hi @Lee see #general"
    assert lines[1] == (
        "1791158460.000200| ↳ [2026-10-05 00:01 UTC Lee] the doc & more [file: plan.pdf]"
    )


def test_ordinary_zip_is_not_slack(tmp_path: Path) -> None:
    path = tmp_path / "a.zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("users.json", "[]")
        z.writestr("readme.txt", "hi")
    path.write_bytes(buf.getvalue())
    assert pick(path)[0] == "archive"


def test_srt_cites_time_spans() -> None:
    kind, out = melt(FIXTURES / "a.srt")
    assert kind == "subtitle"
    assert out.blocks[0].src.cite() == "a.srt@00:00:01.0-00:00:03.5"
    assert out.blocks[0].text.splitlines() == [
        "00:00:01.0-00:00:03.5| Hello there",
        "00:01:02.0-00:01:05.0| Second line",
    ]


def test_vtt_goes_to_subtitle(tmp_path: Path) -> None:
    path = tmp_path / "a.vtt"
    path.write_text("WEBVTT\n\n00:00:02.000 --> 00:00:04.000\n<c>caption</c>\n", encoding="utf-8")
    kind, out = melt(path)
    assert kind == "subtitle"
    assert out.blocks[0].text == "00:00:02.0-00:00:04.0| caption"


def test_ass_dialogue_with_tags_stripped() -> None:
    kind, out = melt(FIXTURES / "a.ass")
    assert kind == "subtitle"
    assert out.blocks[0].text.splitlines() == [
        "00:00:01.0-00:00:03.5| 안녕하세요 반갑습니다",
        "00:01:02.0-00:01:05.0| Commas, inside, text",
    ]


def test_vcf_photo_removed_and_lines_unfolded(tmp_path: Path) -> None:
    path = tmp_path / "a.vcf"
    photo = "A" * 5000
    path.write_text(
        "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:홍길동\r\n"
        f"PHOTO;ENCODING=b;TYPE=JPEG:{photo[:70]}\r\n {photo[70:]}\r\n"
        "NOTE:긴 메모가\r\n  이어짐\r\nEND:VCARD\r\n",
        encoding="utf-8",
    )
    kind, out = melt(path)
    assert kind == "text"
    text = out.blocks[0].text
    assert "AAAA" not in text
    assert "4| PHOTO;ENCODING=b;TYPE=JPEG: [embedded data removed, 5,000 chars]" in text
    assert "6| NOTE:긴 메모가 이어짐" in text
    assert "8| END:VCARD" in text


def test_ics_unfolds(tmp_path: Path) -> None:
    path = tmp_path / "a.ics"
    path.write_text(
        "BEGIN:VEVENT\r\nSUMMARY:주간 회의\r\nDESCRIPTION:접혀\r\n 서 이어짐\r\nEND:VEVENT\r\n",
        encoding="utf-8",
    )
    _, out = melt(path)
    assert "3| DESCRIPTION:접혀서 이어짐" in out.blocks[0].text
