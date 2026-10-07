"""Stream archive members into children without extracting anything by its stored path"""

from __future__ import annotations

import lzma
import shutil
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from importlib.util import find_spec
from pathlib import Path

from meltify import passwords, tools
from meltify.converters import Block, Child, Converted, run, suffix_of
from meltify.converters.archive_backends import (
    NEEDS_7ZIP,
    SINGLE,
    WINZIP_AES,
    Member,
    TooMany,
    bsdtar_members,
    libarchive_members,
    sevenzip_members,
    single_members,
    tar_members,
    zip_members,
)
from meltify.converters.limits import (
    MAX_MEMBER_BYTES,
    MAX_RATIO,
    RATIO_FLOOR,
    human_bytes,
    inflates,
)
from meltify.converters.tables import escape_cell
from meltify.evidence import Src
from meltify.files import RAR_MAGIC, SEVEN_ZIP_MAGIC, member_name

# Shared by every archive nested under one input, so a zip of zips can't multiply it
MAX_TOTAL_BYTES = 1 << 30
# Bigger members go to a temp file instead of memory
MEMBER_SPILL_BYTES = 8 << 20
# How many skipped names a needs line shows before it just counts the rest
SHOW_NAMES = 5

TAR = {".tar", ".tgz", ".tar.gz", ".tbz2", ".tar.bz2", ".txz", ".tar.xz"}
SEVEN_MAGIC = (SEVEN_ZIP_MAGIC, RAR_MAGIC)
NEEDS_EXTRA = "archive extra (meltify doctor --install archive)"


class Skip(Exception):
    """One member stays out and the rest go on"""


class Stop(Exception):
    """The archive as a whole hit a limit, so no more members are read"""


@dataclass
class Meter:
    """Counts decompressed bytes against this archive and its whole input tree"""

    key: str
    size: int  # the archive file on disk
    # Tar and libarchive formats store no per-member compressed size, and skipping a member
    # still decompresses it, so they're metered as one stream
    streaming: bool
    budget: run.Budget = field(default_factory=run.Budget)
    read: int = 0

    def reserve(self, declared: int) -> None:
        if self.budget.add(self.key, 0) + declared > MAX_TOTAL_BYTES:
            raise Stop(f"total over {human_bytes(MAX_TOTAL_BYTES)}")

    def charge(self, n: int) -> None:
        self.read += n
        if self.budget.add(self.key, n) > MAX_TOTAL_BYTES:
            raise Stop(f"total over {human_bytes(MAX_TOTAL_BYTES)}")
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
            if size > MAX_MEMBER_BYTES:
                raise Skip(f"over {human_bytes(MAX_MEMBER_BYTES)}")
            if inflates(size, member.packed):
                raise Skip(f"expands over {MAX_RATIO}:1")
            if sink is None and size > MEMBER_SPILL_BYTES:
                # In a workdir, so a member read never moves out, like one past the nesting
                # limit, still goes when the run ends
                spill = run.workdir("meltify-spill-") / f"member{Path(member.name).suffix}"
                sink = spill.open("wb")
                sink.write(buf)
                buf = bytearray()
            if sink is None:
                buf += chunk
            else:
                sink.write(chunk)
    except BaseException:
        if sink is not None:
            sink.close()
            shutil.rmtree(spill.parent, ignore_errors=True)
        raise
    if sink is None:
        return bytes(buf)
    sink.close()
    return spill


def _take(member: Member, meter: Meter) -> bytes | Path:
    size = member.size
    if size is not None and size > MAX_MEMBER_BYTES:
        raise Skip(f"over {human_bytes(MAX_MEMBER_BYTES)}")
    if size is not None and inflates(size, member.packed):
        raise Skip(f"expands over {MAX_RATIO}:1")
    meter.reserve(size or 0)
    return _drain(member, meter)


def _aes_for_7zip(path: Path) -> bool:
    """A zip with AES members and a password, which only 7-Zip can open without pyzipper"""
    if not passwords.password() or tools.seven_zip() is None or find_spec("pyzipper"):
        return False
    with zipfile.ZipFile(path) as z:
        return any(i.compress_type == WINZIP_AES for i in z.infolist())


def _seven_reader() -> Callable[[Path], Iterator[Member]] | None:
    """7z and RAR: libarchive, then 7-Zip, then bsdtar

    7-Zip goes first when a password is set, since only it decrypts these
    """
    seven = tools.seven_zip() is not None
    if seven and (passwords.password() or not libarchive_ready()):
        return sevenzip_members
    if libarchive_ready():
        return libarchive_members
    if shutil.which("bsdtar"):
        return bsdtar_members
    return None


def _wraps_tar(path: Path) -> bool:
    # A tarball named plain `.gz` still has the ustar magic 257 bytes into its stream
    try:
        with SINGLE[suffix_of(path)](path, "rb") as f:
            return f.read(512)[257:262] == b"ustar"
    except (OSError, EOFError, lzma.LZMAError):
        return False


def libarchive_ready() -> bool:
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
    if suffix in SINGLE:
        return "tar" if _wraps_tar(path) else "single"
    with path.open("rb") as f:
        head = f.read(8)
    if suffix in (".7z", ".rar") or head.startswith(SEVEN_MAGIC):
        return "7z"
    if suffix == ".zip" or head.startswith(b"PK"):
        return "zip"
    return "tar"


def _unreadable(e: Exception) -> str:
    # libarchive errors carry a pointer and errno after the message, which only add noise
    if isinstance(e, passwords.WrongPassword):
        return passwords.WRONG
    text = str(e.args[0]) if e.args else str(e)
    if "ncrypt" in text or "assphrase" in text:
        # With a password set, 7-Zip would have read it, so only libarchive got here
        return passwords.LOCKED if tools.seven_zip() else NEEDS_7ZIP
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
        lines.append(
            f"| {escape_cell(name)} | {'' if size is None else size} | {escape_cell(note)} |"
        )
    return Block(src, "\n".join(lines))


def _discard(children: list[Child]) -> None:
    for child in children:
        if child.path is not None:
            shutil.rmtree(child.path.parent, ignore_errors=True)


def convert(path: Path, src: Src) -> Converted:
    out = Converted("archive")
    kind = _kind(path)
    if kind == "7z":
        reader = _seven_reader()
    elif kind == "zip":
        reader = sevenzip_members if _aes_for_7zip(path) else zip_members
    else:
        reader = tar_members
    if reader is None:
        out.needs.append(NEEDS_EXTRA)
        return out
    if kind == "single":
        # A nested file sits on disk under a flattened name, so the inner name comes from the cite
        members = single_members(path, Path(src.parts[-1] if src.parts else src.path).stem)
    else:
        members = reader(path)
    # Only zip stores a compressed size per member, so the rest are metered as one stream.
    # Every archive nested under one input in a run shares that input's budget
    meter = Meter(
        src.path,
        path.stat().st_size,
        streaming=reader is not zip_members,
        budget=run.current().budget,
    )
    listing = Listing()
    try:
        return _melt(members, src, meter, listing, out)
    finally:
        # Closing the generator closes the archive and stops any tool still unpacking it
        members.close()


def _melt(
    members: Iterator[Member], src: Src, meter: Meter, listing: Listing, out: Converted
) -> Converted:
    try:
        for i, m in enumerate(members, start=1):
            # read cleans the name the same way before it reaches a cite
            clean = member_name(m.name, i)
            try:
                if m.skip:
                    raise Skip(m.skip)
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
            except passwords.Locked:
                # A wrong password ends the whole stream, not just this member
                raise
            except Exception as e:  # noqa: BLE001
                reason = _unreadable(e)
                # A stream can't step past a member it couldn't decrypt
                if meter.streaming and reason in (passwords.LOCKED, NEEDS_7ZIP):
                    raise passwords.Locked(reason) from e
                # A broken or password-protected member shouldn't hide the readable ones
                listing.skip(clean, reason, m.size)
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
        locked = isinstance(e, passwords.Locked)
        reason = str(e) if locked else _unreadable(e)
        if locked or reason in (passwords.LOCKED, NEEDS_7ZIP):
            # Empty files and links need no key, so a locked archive can still list a few
            if not listing.rows:
                out.needs.append(reason)
                return out
            listing.needs.append(reason)
        elif not listing.rows:
            raise
        else:
            # Keep what came out before a truncated or corrupt tail
            listing.needs.append(f"archive ended early ({reason})")

    out.blocks.append(_listing_block(src, listing))
    out.needs += listing.all_needs()
    return out
