"""Stream archive members into children without extracting anything by its stored path"""

from __future__ import annotations

import os
import stat
import tarfile
import tempfile
import threading
import zipfile
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from meltify.commands.read import member_name
from meltify.converters import Block, Child, Converted, suffix_of
from meltify.evidence import Src

MAX_ENTRIES = 10_000
MAX_ENTRY_BYTES = 256 << 20
# Shared by every archive nested under one input, so a zip of zips can't multiply it
MAX_TOTAL_BYTES = 1 << 30
MAX_RATIO = 100
# Text compresses well, so the ratio only applies to members big enough to hurt
RATIO_FLOOR = 1 << 20
# Bigger members go to a temp file instead of memory
SPILL_BYTES = 8 << 20
CHUNK = 1 << 20
# How many skipped names a needs line shows before it just counts the rest
SHOW_NAMES = 5

TAR = {".tar", ".tgz", ".tar.gz", ".tbz2", ".tar.bz2", ".txz", ".tar.xz"}
LIBARCHIVE_MAGIC = (b"7z\xbc\xaf\x27\x1c", b"Rar!\x1a\x07")
NEEDS_EXTRA = "archive extra (meltify doctor --install archive)"

_spent: dict[str, int] = {}
_spent_lock = threading.Lock()


def _amount(n: int) -> str:
    for unit, shift in (("GiB", 30), ("MiB", 20)):
        if n >= 1 << shift:
            return f"{n / (1 << shift):g} {unit}"
    return f"{n} bytes"


class Skip(Exception):
    """One member stays out and the rest go on"""


class Stop(Exception):
    """The archive as a whole hit a limit, so no more members are read"""


class TooMany(Exception):
    pass


@dataclass
class Member:
    name: str
    size: int | None = None  # declared uncompressed size, when the format stores one
    packed: int | None = None  # compressed size, zip only
    skip: str | None = None
    chunks: Callable[[], Iterable[bytes]] | None = None


@dataclass
class Meter:
    """Counts decompressed bytes against this archive and its whole input tree"""

    key: str
    size: int  # the archive file on disk
    # Tar and libarchive formats store no per-member compressed size, and skipping a member
    # still decompresses it, so they're metered as one stream
    streaming: bool
    read: int = 0

    def reserve(self, declared: int) -> None:
        with _spent_lock:
            if _spent.get(self.key, 0) + declared > MAX_TOTAL_BYTES:
                raise Stop(f"total over {_amount(MAX_TOTAL_BYTES)}")

    def charge(self, n: int) -> None:
        self.read += n
        with _spent_lock:
            _spent[self.key] = _spent.get(self.key, 0) + n
            total = _spent[self.key]
        if total > MAX_TOTAL_BYTES:
            raise Stop(f"total over {_amount(MAX_TOTAL_BYTES)}")
        if self.streaming and self.read > RATIO_FLOOR and self.read > self.size * MAX_RATIO:
            raise Stop(f"expands over {MAX_RATIO}:1")

    def afford(self, n: int) -> bool:
        try:
            self.charge(n)
        except Stop:
            return False
        return True


@dataclass
class Listing:
    rows: list[tuple[str, int | None, str]] = field(default_factory=list)
    skipped: dict[str, list[str]] = field(default_factory=dict)
    needs: list[str] = field(default_factory=list)

    def skip(self, name: str, reason: str, size: int | None = None) -> None:
        self.rows.append((name, size, f"skipped: {reason}"))
        self.skipped.setdefault(reason, []).append(name)

    def all_needs(self) -> list[str]:
        out = []
        for reason, names in self.skipped.items():
            shown = ", ".join(names[:SHOW_NAMES])
            more = len(names) - SHOW_NAMES
            out.append(f"skipped {shown}{f' and {more} more' if more > 0 else ''} ({reason})")
        return out + self.needs


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _inflated(size: int, packed: int | None) -> bool:
    return packed is not None and size > RATIO_FLOOR and size > packed * MAX_RATIO


def _drain(member: Member, meter: Meter) -> bytes | Path:
    buf = bytearray()
    spill: Path | None = None
    sink = None
    size = 0
    try:
        for chunk in member.chunks():
            size += len(chunk)
            meter.charge(len(chunk))
            # Declared sizes can lie, so the bytes actually read are checked too
            if size > MAX_ENTRY_BYTES:
                raise Skip(f"over {_amount(MAX_ENTRY_BYTES)}")
            if _inflated(size, member.packed):
                raise Skip(f"expands over {MAX_RATIO}:1")
            if sink is None and size > SPILL_BYTES:
                fd, name = tempfile.mkstemp(prefix="meltify-", suffix=Path(member.name).suffix)
                spill, sink = Path(name), os.fdopen(fd, "wb")
                sink.write(buf)
                buf = bytearray()
            if sink is None:
                buf += chunk
            else:
                sink.write(chunk)
    except BaseException:
        if sink is not None:
            sink.close()
            spill.unlink(missing_ok=True)
        raise
    if sink is None:
        return bytes(buf)
    sink.close()
    return spill


def _take(member: Member, meter: Meter) -> bytes | Path:
    size = member.size
    if size is not None and size > MAX_ENTRY_BYTES:
        raise Skip(f"over {_amount(MAX_ENTRY_BYTES)}")
    if size is not None and _inflated(size, member.packed):
        raise Skip(f"expands over {MAX_RATIO}:1")
    meter.reserve(size or 0)
    return _drain(member, meter)


def _tar_name(name: str) -> str:
    # tarfile decodes as UTF-8 with surrogates, which a Korean Windows tool's cp949 names break
    try:
        name.encode("utf-8")
        return name
    except UnicodeEncodeError:
        raw = name.encode("utf-8", "surrogateescape")
    try:
        return raw.decode("cp949")
    except UnicodeDecodeError:
        return raw.decode("utf-8", "replace")


def _zip_name(info: zipfile.ZipInfo) -> str:
    # Without the UTF-8 flag, zipfile decodes as cp437, but Korean Windows writes cp949
    if info.flag_bits & 0x800:
        return info.filename
    raw = info.filename.encode("cp437")
    for encoding in ("utf-8", "cp949"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return info.filename


def _special(mode: int) -> str | None:
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISCHR(mode) or stat.S_ISBLK(mode) or stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode):
        return "special file"
    return None


def _zip_members(path: Path) -> Iterator[Member]:
    with zipfile.ZipFile(path) as z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        if len(infos) > MAX_ENTRIES:
            raise TooMany(f"{len(infos)} entries, over the {MAX_ENTRIES} limit")
        for info in infos:
            name = _zip_name(info)
            special = _special(info.external_attr >> 16) if info.create_system == 3 else None
            if special:
                yield Member(name, skip=special)
            elif info.flag_bits & 0x1:
                yield Member(name, info.file_size, skip="encrypted")
            else:

                def chunks(info: zipfile.ZipInfo = info) -> Iterator[bytes]:
                    with z.open(info) as f:
                        yield from iter(lambda: f.read(CHUNK), b"")

                yield Member(name, info.file_size, info.compress_size, chunks=chunks)


def _tar_members(path: Path) -> Iterator[Member]:
    # Stream mode reads members in order and never seeks or extracts by name
    with tarfile.open(path, "r|*") as t:
        count = 0
        for m in t:
            if m.isdir():
                continue
            count += 1
            if count > MAX_ENTRIES:
                raise TooMany(f"over {MAX_ENTRIES} entries")
            name = _tar_name(m.name)
            if m.issym() or m.islnk():
                yield Member(name, skip="symlink" if m.issym() else "hard link")
            elif not m.isreg():
                yield Member(name, skip="special file")
            else:

                def chunks(m: tarfile.TarInfo = m) -> Iterator[bytes]:
                    f = t.extractfile(m)
                    yield from iter(lambda: f.read(CHUNK), b"")

                yield Member(name, m.size, chunks=chunks)


def _libarchive_members(path: Path) -> Iterator[Member]:
    import libarchive

    with libarchive.file_reader(str(path)) as archive:
        count = 0
        for entry in archive:
            if entry.isdir:
                continue
            count += 1
            if count > MAX_ENTRIES:
                raise TooMany(f"over {MAX_ENTRIES} entries")
            name = entry.pathname or ""
            if entry.issym or entry.islnk:
                yield Member(name, skip="symlink" if entry.issym else "hard link")
            elif not entry.isreg:
                yield Member(name, skip="special file")
            else:
                yield Member(name, entry.size, chunks=entry.get_blocks)


def _libarchive_ready() -> bool:
    try:
        import libarchive  # noqa: F401
    except (ImportError, OSError, AttributeError):
        # The Python package loads the system libarchive at import, which can be absent too
        return False
    return True


def _kind(path: Path) -> str:
    suffix = suffix_of(path)
    if suffix in TAR:
        return "tar"
    with path.open("rb") as f:
        head = f.read(8)
    if suffix in (".7z", ".rar") or head.startswith(LIBARCHIVE_MAGIC):
        return "libarchive"
    if suffix == ".zip" or head.startswith(b"PK"):
        return "zip"
    return "tar"


def _unreadable(e: Exception) -> str:
    # libarchive errors carry a pointer and errno after the message, which only add noise
    text = str(e.args[0]) if e.args else str(e)
    if "ncrypt" in text or "assphrase" in text:
        return "encrypted"
    return f"unreadable, {type(e).__name__}: {text}"[:120]


def _listing_block(src: Src, listing: Listing) -> Block:
    kept = sum(1 for _, _, note in listing.rows if not note.startswith("skipped"))
    lines = [
        f"{len(listing.rows)} members, {kept} read",
        "",
        "| member | bytes | note |",
        "|---|---|---|",
    ]
    for name, size, note in listing.rows:
        lines.append(f"| {_cell(name)} | {'' if size is None else size} | {_cell(note)} |")
    return Block(src, "\n".join(lines))


def _discard(children: list[Child]) -> None:
    for child in children:
        if child.path is not None:
            child.path.unlink(missing_ok=True)


def convert(path: Path, src: Src) -> Converted:
    out = Converted("archive")
    kind = _kind(path)
    if kind == "libarchive" and not _libarchive_ready():
        out.needs.append(NEEDS_EXTRA)
        return out
    members = {"zip": _zip_members, "tar": _tar_members, "libarchive": _libarchive_members}[kind]
    # Archives nested in this input share one budget. A new top-level input starts fresh
    if not src.parts:
        with _spent_lock:
            _spent[src.path] = 0
    meter = Meter(src.path, path.stat().st_size, streaming=kind != "zip")
    listing = Listing()

    try:
        for i, m in enumerate(members(path), start=1):
            # read cleans the name the same way before it reaches a cite
            clean = member_name(m.name, i)
            if m.skip:
                listing.skip(clean, m.skip, m.size)
                continue
            try:
                body = _take(m, meter)
            except Skip as e:
                listing.skip(clean, str(e), m.size)
                # The stream still inflates a skipped member to reach the next header
                if meter.streaming and m.size and not meter.afford(m.size):
                    listing.needs.append(f"stopped after {clean} (total or ratio limit)")
                    break
                continue
            except Stop as e:
                listing.needs.append(f"stopped at {clean} ({e}), later members unread")
                break
            except Exception as e:  # noqa: BLE001
                # A broken or password-protected member shouldn't hide the readable ones
                listing.skip(clean, _unreadable(e), m.size)
                continue
            size = len(body) if isinstance(body, bytes) else body.stat().st_size
            # Traversal, roots, drive letters and backslashes all lose their meaning here
            note = "" if clean == m.name else f"cited as {clean}"
            listing.rows.append((m.name, size, note))
            if isinstance(body, bytes):
                out.children.append(Child(m.name, src, data=body))
            else:
                out.children.append(Child(m.name, src, path=body))
    except TooMany as e:
        _discard(out.children)
        out.children = []
        out.needs.append(f"archive rejected: {e}")
        return out
    except Exception as e:  # noqa: BLE001
        reason = _unreadable(e)
        if not listing.rows and reason == "encrypted":
            out.needs.append("encrypted archive (meltify can't read it without the password)")
            return out
        if not listing.rows:
            raise
        # Keep what came out before a truncated or corrupt tail
        listing.needs.append(f"archive ended early ({reason})")

    out.blocks.append(_listing_block(src, listing))
    out.needs += listing.all_needs()
    return out
