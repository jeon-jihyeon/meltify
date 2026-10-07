"""Archive readers that stream each member's bytes, one per format and tool"""

from __future__ import annotations

import bz2
import gzip
import lzma
import stat
import subprocess
import tarfile
import tempfile
import threading
import zipfile
import zlib
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from meltify import passwords, safe, tools
from meltify.converters import suffix_of

MAX_ENTRIES = 10_000
CHUNK = 1 << 20
# One compressed stream with no member list, so the file inside takes the outer name
SINGLE = {".gz": gzip.open, ".bz2": bz2.open, ".xz": lzma.open}
# libarchive and bsdtar open 7z and RAR but can't decrypt them
NEEDS_7ZIP = "encrypted, needs 7-Zip and a password (meltify doctor --install archive)"
# Zip's compression method id for WinZip AES, which stdlib zipfile can't decrypt
WINZIP_AES = 99
LIST_TIMEOUT = 60
# A big archive takes a while to unpack, but a stuck tool shouldn't hold up the run
EXTRACT_TIMEOUT = 900


class TooMany(Exception):
    pass


@dataclass
class Member:
    name: str
    size: int | None = None  # declared uncompressed size, when the format stores one
    packed: int | None = None  # compressed size, zip only
    skip: str | None = None
    chunks: Callable[[], Iterable[bytes]] | None = None


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
    # A zipfile fork that also reads WinZip AES, WinZip's default and an option in 7-Zip
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


def zip_members(path: Path) -> Iterator[Member]:
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


def tar_members(path: Path) -> Iterator[Member]:
    # Stream mode reads members in order and never seeks or extracts by name
    with tarfile.open(path, "r|*") as t:
        yield from _tar_stream(t)


def libarchive_members(path: Path) -> Iterator[Member]:
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


def sevenzip_members(path: Path) -> Iterator[Member]:
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


def bsdtar_members(path: Path) -> Iterator[Member]:
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


def single_members(path: Path, name: str) -> Iterator[Member]:
    def chunks() -> Iterator[bytes]:
        with SINGLE[suffix_of(path)](path, "rb") as f:
            yield from iter(lambda: f.read(CHUNK), b"")

    yield Member(name, chunks=chunks)
