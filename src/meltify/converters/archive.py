"""Stream archive members into children without extracting anything by its stored path"""

from __future__ import annotations

import bz2
import gzip
import lzma
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
import zipfile
import zlib
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from importlib.util import find_spec
from pathlib import Path
from typing import IO

from meltify import passwords, safe, tools
from meltify.converters import Block, Child, Converted, run, suffix_of
from meltify.converters.limits import (
    MAX_MEMBER_BYTES,
    MAX_RATIO,
    RATIO_FLOOR,
    human_bytes,
    inflates,
)
from meltify.converters.tables import escape_cell
from meltify.evidence import Src
from meltify.files import member_name

MAX_ENTRIES = 10_000
# Shared by every archive nested under one input, so a zip of zips can't multiply it
MAX_TOTAL_BYTES = 1 << 30
# Bigger members go to a temp file instead of memory
SPILL_BYTES = 8 << 20
CHUNK = 1 << 20
# How many skipped names a needs line shows before it just counts the rest
SHOW_NAMES = 5

TAR = {".tar", ".tgz", ".tar.gz", ".tbz2", ".tar.bz2", ".txz", ".tar.xz"}
# One compressed stream with no member list, so the file inside takes the outer name
SINGLE = {".gz": gzip.open, ".bz2": bz2.open, ".xz": lzma.open}
SEVEN_MAGIC = (b"7z\xbc\xaf\x27\x1c", b"Rar!\x1a\x07")
NEEDS_EXTRA = "archive extra (meltify doctor --install archive)"
# libarchive and bsdtar open 7z and RAR but can't decrypt them
NEEDS_7ZIP = "encrypted, needs 7-Zip and a password (meltify doctor --install archive)"
# Zip's compression method id for WinZip AES, which stdlib zipfile can't decrypt
WINZIP_AES = 99
LIST_TIMEOUT = 60
# A big archive takes a while to unpack, but a stuck tool shouldn't hold up the run
EXTRACT_TIMEOUT = 900


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
            if sink is None and size > SPILL_BYTES:
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


def _zip_file(path: Path) -> zipfile.ZipFile:
    try:
        import pyzipper
    except ImportError:
        return zipfile.ZipFile(path)
    # A zipfile fork that also reads WinZip AES, the encryption 7-Zip and WinZip default to
    return pyzipper.AESZipFile(path)


def _zip_chunks(z: zipfile.ZipFile, info: zipfile.ZipInfo, secret: str | None) -> Iterator[bytes]:
    pwd = secret.encode() if secret and info.flag_bits & 0x1 else None
    try:
        with z.open(info, pwd=pwd) as f:
            yield from iter(lambda: f.read(CHUNK), b"")
    except (zipfile.BadZipFile, zlib.error) as e:
        if pwd is None:
            raise
        # ZipCrypto checks only one byte of the key, so a wrong password often gets this far
        raise passwords.WrongPassword from e
    except RuntimeError as e:
        # zipfile and pyzipper say a password check failed only in the message
        if pwd is None or "Bad password" not in str(e):
            raise
        raise passwords.WrongPassword from e


def _zip_members(path: Path) -> Iterator[Member]:
    secret = passwords.password()
    with _zip_file(path) as z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        if len(infos) > MAX_ENTRIES:
            raise TooMany(f"{len(infos)} entries, over the {MAX_ENTRIES} limit")
        for info in infos:
            name = _zip_name(info)
            special = _special(info.external_attr >> 16) if info.create_system == 3 else None
            if special:
                yield Member(name, skip=special)
            elif info.flag_bits & 0x1 and secret is None:
                yield Member(name, info.file_size, skip=passwords.LOCKED)
            elif info.compress_type == WINZIP_AES:
                yield Member(name, info.file_size, skip=passwords.NEEDS_CRYPTO)
            else:

                def chunks(info: zipfile.ZipInfo = info) -> Iterator[bytes]:
                    return _zip_chunks(z, info, secret)

                yield Member(name, info.file_size, info.compress_size, chunks=chunks)


def _aes_for_7zip(path: Path) -> bool:
    """A zip with AES members and a password, which only 7-Zip can open without pyzipper"""
    if not passwords.password() or tools.seven_zip() is None or find_spec("pyzipper"):
        return False
    with zipfile.ZipFile(path) as z:
        return any(i.compress_type == WINZIP_AES for i in z.infolist())


def _tar_stream(t: tarfile.TarFile) -> Iterator[Member]:
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


def _tar_members(path: Path) -> Iterator[Member]:
    # Stream mode reads members in order and never seeks or extracts by name
    with tarfile.open(path, "r|*") as t:
        yield from _tar_stream(t)


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


def _failure(tool: str, output: bytes) -> Exception:
    text = output.decode("utf-8", "replace")
    if "Wrong password" in text:
        # p7zip takes a closed stdin as an empty password, so with none set it's only locked
        return passwords.Locked(passwords.WRONG if passwords.password() else passwords.LOCKED)
    # 7-Zip asks for a password on stdin and gives up at end of input
    if "Enter password" in text or "Break signaled" in text:
        return passwords.Locked(passwords.LOCKED)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return OSError(f"{tool}: {' '.join(lines[-2:]) or 'failed'}"[:200])


@contextmanager
def _spawn(
    cmd: list[str], secret: str | None = None
) -> Iterator[tuple[subprocess.Popen, IO[bytes]]]:
    """A tool streaming to stdout, killed on timeout or once the caller stops reading

    The password goes in on stdin, since argv shows up in the process list. Errors go to a
    temp file, so a chatty tool can't fill a pipe nobody reads and stall
    """
    with tempfile.TemporaryFile() as err:
        # Its own session, so a kill also reaches any helper the tool started
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE if secret else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=err,
            start_new_session=True,
        )
        timer = threading.Timer(EXTRACT_TIMEOUT, safe.kill_group, (proc,))
        timer.start()
        try:
            if secret:
                try:
                    proc.stdin.write(f"{secret}\n".encode())
                    proc.stdin.close()
                except BrokenPipeError:
                    pass
            yield proc, err
        finally:
            timer.cancel()
            if proc.poll() is None:
                safe.kill_group(proc)
            proc.stdout.close()
            proc.wait()


def _errors(proc: subprocess.Popen, err: IO[bytes]) -> bytes:
    proc.wait()
    err.seek(0)
    return err.read()


def _sevenzip_list(exe: str, path: Path, secret: str | None) -> list[dict[str, str]]:
    cmd = [exe, "l", "-slt", "-ba", "-bd", "-sccUTF-8", "-spd", "--", str(path)]
    r = safe.run(
        cmd,
        input=f"{secret}\n".encode() if secret else None,
        timeout=LIST_TIMEOUT,
        check=False,
        text=False,
    )
    if r.returncode:
        raise _failure("7-Zip", r.stdout + r.stderr)
    entries: list[dict[str, str]] = []
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        key, sep, value = line.partition(" = ")
        if not sep:
            continue
        if key == "Path":
            entries.append({})
        if entries:
            entries[-1][key] = value
    return entries


def _sevenzip_skip(entry: dict[str, str]) -> str | None:
    # The mode string sits in Attributes for 7z and RAR and in Mode for tar
    mode = (entry.get("Mode") or entry.get("Attributes", "").rpartition(" ")[2])[:1]
    if mode == "l" or entry.get("Symbolic Link"):
        return "symlink"
    if entry.get("Hard Link"):
        return "hard link"
    if mode in ("c", "b", "p", "s"):
        return "special file"
    return None


def _sevenzip_members(path: Path) -> Iterator[Member]:
    """7z, RAR and AES zip through 7-Zip, without anything landing on disk by its stored name

    One `x -so` run writes every file to stdout back to back in listing order, each exactly
    its listed size, so the stream is cut by those sizes. Symlinks come through as their
    target, which is read past like any skipped member
    """
    exe = tools.seven_zip()
    secret = passwords.password()
    entries = [
        e
        for e in _sevenzip_list(exe, path, secret)
        if e.get("Folder") != "+" and not e.get("Attributes", "").startswith("D")
    ]
    if len(entries) > MAX_ENTRIES:
        raise TooMany(f"{len(entries)} entries, over the {MAX_ENTRIES} limit")
    if secret is None and any(e.get("Encrypted") == "+" for e in entries):
        raise passwords.Locked(passwords.LOCKED)
    cmd = [exe, "x", "-so", "-bd", "-y", "-spd", "--", str(path)]
    with _spawn(cmd, secret) as (proc, err):
        left = 0

        def take(n: int) -> Iterator[bytes]:
            nonlocal left
            while left and n:
                chunk = proc.stdout.read(min(CHUNK, left, n))
                if not chunk:
                    raise _failure("7-Zip", _errors(proc, err))
                left -= len(chunk)
                n -= len(chunk)
                yield chunk

        for e in entries:
            # Whatever the previous member left unread stands between it and this one
            for _ in take(left):
                pass
            if not e.get("Size", "").isdigit():
                raise OSError(f"7-Zip lists no size for {e.get('Path')}")
            left = int(e["Size"])
            skip = _sevenzip_skip(e)
            if skip:
                # Its bytes still come down the stream, so the size lets the meter charge them
                yield Member(e.get("Path", ""), left, skip=skip)
            else:
                yield Member(e.get("Path", ""), left, chunks=lambda size=left: take(size))
        for _ in take(left):
            pass
        if proc.stdout.read(1) or proc.wait():
            raise _failure("7-Zip", _errors(proc, err))


def _bsdtar_members(path: Path) -> Iterator[Member]:
    """7z and RAR through bsdtar, which macOS ships, rewritten as a tar stream

    The tar reader then applies the same member checks as any tarball
    """
    cmd = ["bsdtar", "-cf", "-", "--format", "pax", f"@{path.absolute()}"]
    with _spawn(cmd) as (proc, err):
        try:
            with tarfile.open(fileobj=proc.stdout, mode="r|") as t:
                yield from _tar_stream(t)
        except tarfile.ReadError as e:
            raise _bsdtar_failure(_errors(proc, err)) from e
        if proc.wait():
            raise _bsdtar_failure(_errors(proc, err))


def _bsdtar_failure(output: bytes) -> Exception:
    text = output.decode("utf-8", "replace")
    lines = [line.removeprefix("bsdtar: ").strip() for line in text.splitlines()]
    detail = next((x for x in lines if x and not x.startswith("Error exit")), "")
    # bsdtar takes a password only in argv, so 7-Zip is the way in. Encrypted 7z content
    # fails with no message at all
    if "ncrypt" in detail or detail in ("", "(null)"):
        return passwords.Locked(NEEDS_7ZIP)
    return OSError(f"bsdtar: {detail}"[:200])


def _seven_reader() -> Callable[[Path], Iterator[Member]] | None:
    """7z and RAR: libarchive, then 7-Zip, then bsdtar

    7-Zip goes first when a password is set, since only it decrypts these
    """
    seven = tools.seven_zip() is not None
    if seven and (passwords.password() or not libarchive_ready()):
        return _sevenzip_members
    if libarchive_ready():
        return _libarchive_members
    if shutil.which("bsdtar"):
        return _bsdtar_members
    return None


def _single_members(path: Path, name: str) -> Iterator[Member]:
    def chunks() -> Iterator[bytes]:
        with SINGLE[suffix_of(path)](path, "rb") as f:
            yield from iter(lambda: f.read(CHUNK), b"")

    yield Member(name, chunks=chunks)


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
        reader = _sevenzip_members if _aes_for_7zip(path) else _zip_members
    else:
        reader = _tar_members
    if reader is None:
        out.needs.append(NEEDS_EXTRA)
        return out
    if kind == "single":
        # A nested file sits on disk under a flattened name, so the inner name comes from the cite
        members = _single_members(path, Path(src.parts[-1] if src.parts else src.path).stem)
    else:
        members = reader(path)
    # Only zip stores a compressed size per member, so the rest are metered as one stream.
    # Every archive nested under one input in a run shares that input's budget
    meter = Meter(
        src.path,
        path.stat().st_size,
        streaming=reader is not _zip_members,
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
