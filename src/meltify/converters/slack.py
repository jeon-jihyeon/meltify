"""Slack workspace export zips: users.json, channels.json and one JSON per channel day"""

from __future__ import annotations

import json
import re
import zipfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from meltify.converters import Block, Converted
from meltify.evidence import Src

DAY_FILE = re.compile(r"^([^/]+)/(\d{4}-\d{2}-\d{2})\.json$")
ROSTERS = ("users.json", "channels.json", "groups.json", "mpims.json", "dms.json")
BLOCK_MESSAGES = 200
# Day files are small, but a hostile zip could claim anything
MAX_MEMBER_BYTES = 50_000_000


def sniff(path: Path, head: bytes) -> bool:
    if not head.startswith(b"PK\x03\x04"):
        return False
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
    except (zipfile.BadZipFile, OSError):
        return False
    roster = {"users.json", "channels.json"} & set(names)
    return bool(roster) and any(DAY_FILE.match(n) for n in names)


def _load(z: zipfile.ZipFile, name: str) -> list | dict | None:
    try:
        info = z.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_MEMBER_BYTES:
        return None
    try:
        return json.loads(z.read(info))
    except ValueError:
        return None


def _names(z: zipfile.ZipFile) -> dict[str, str]:
    names: dict[str, str] = {}
    for user in _load(z, "users.json") or []:
        profile = user.get("profile", {})
        names[user.get("id", "")] = (
            profile.get("display_name") or user.get("real_name") or user.get("name") or ""
        )
    for roster in ROSTERS[1:]:
        for ch in _load(z, roster) or []:
            names.setdefault(ch.get("id", ""), ch.get("name", ""))
    return {k: v for k, v in names.items() if k and v}


def _clean(text: str, names: dict[str, str]) -> str:
    def mention(m: re.Match) -> str:
        target, _, label = m.group(1).partition("|")
        if target.startswith("@"):
            return "@" + (names.get(target[1:]) or label or target[1:])
        if target.startswith("#"):
            return "#" + (label or names.get(target[1:]) or target[1:])
        if target.startswith("!"):
            return "@" + (label or target[1:].split("^")[0])
        return label or target

    text = re.sub(r"<([^<>]+)>", mention, text)
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _line(msg: dict, names: dict[str, str]) -> str:
    when = datetime.fromtimestamp(float(msg["ts"]), UTC).strftime("%Y-%m-%d %H:%M")
    who = (
        names.get(msg.get("user", ""))
        or msg.get("user_profile", {}).get("display_name")
        or msg.get("user_profile", {}).get("real_name")
        or msg.get("username")
        or msg.get("user")
        or msg.get("bot_id")
        or ""
    )
    text = " ".join(_clean(msg.get("text", ""), names).splitlines())
    files = [f.get("name") or f.get("title") for f in msg.get("files", [])]
    text += "".join(f" [file: {f}]" for f in files if f)
    thread = msg.get("thread_ts")
    reply = "↳ " if thread and thread != msg["ts"] else ""
    return f"{msg['ts']}| {reply}[{when} UTC {who}] {text}".rstrip()


def convert(path: Path, src: Src) -> Converted:
    out = Converted("chat")
    with zipfile.ZipFile(path) as z:
        names = _names(z)
        days = sorted(n for n in z.namelist() if DAY_FILE.match(n))
        for member in days:
            messages = _load(z, member)
            if not isinstance(messages, list):
                out.needs.append(f"unreadable {member}")
                continue
            messages = [m for m in messages if isinstance(m, dict) and m.get("ts")]
            day_src = src.inside(member)
            for i in range(0, len(messages), BLOCK_MESSAGES):
                chunk = messages[i : i + BLOCK_MESSAGES]
                body = "\n".join(_line(m, names) for m in chunk)
                out.blocks.append(Block(replace(day_src, ts=chunk[0]["ts"]), body))
    if not out.blocks:
        out.needs.append("no channel messages found")
    return out
