"""Decrypt password-protected PDF and Office files into a temp copy their converter reads

Converters, the OCR stage and the hidden text scan all reopen a document by its path, so
one decrypted copy serves them all and none of them has to know about passwords. A file
this can't open or follow, empty or broken, passes through as it is for its converter to
report
"""

from __future__ import annotations

import mmap
import re
import shutil
import struct
from pathlib import Path
from typing import Any

from meltify.converters import run, sealed_natively, suffix_of
from meltify.needs import error_note
from meltify.passwords import LOCKED, NEEDS_CRYPTO, WRONG, Locked, cant_decrypt, password

OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
# Zip packages Office saves as an OLE container only when a password encrypts them
PACKAGES = {
    ".docx", ".docm", ".dotx", ".dotm", ".pptx", ".pptm", ".potx", ".potm", ".ppsx", ".ppsm",
    ".xlsx", ".xlsm", ".xltx", ".xltm", ".xlsb",
}  # fmt: skip
# Excel encrypts a workbook that is only write-protected with this built-in password
EXCEL_DEFAULT = "VelvetSweatshop"
MAX_MANIFEST_BYTES = 1 << 20
# Bytes a seal check sees, enough for DRM wrappers that name themselves near the start
SEAL_HEAD = 1024
# A linearized PDF's first trailer sits within its first page's objects
HEAD_WINDOW = 1 << 20
# Room for the trailer or xref stream dictionary, and the startxref line after it
TRAILER_WINDOW = 64 << 10
# Office 97 encryption markers
FIB_ENCRYPTED = 0x0100  # fEncrypted in the Word FIB flags
BIFF_FILEPASS = 0x002F
BIFF_EOF = 0x000A
PPT_ENCRYPTED = 0xF3D1C4DF  # Current User header token of an encrypted deck


def unlock(path: Path) -> Path:
    """The path to convert, a decrypted copy when `path` is encrypted

    The copy keeps the original name, so the same converter picks it up, in a folder the
    caller removes once nothing reads it anymore. Raises Locked with the needs line when
    the file stays shut
    """
    try:
        with path.open("rb") as f:
            head = f.read(len(OLE))
    except OSError:
        return path
    if head.startswith(b"%PDF-"):
        return _pdf(path)
    if head.startswith(OLE):
        return _office(path)
    return path


def _names_encryption(path: Path) -> bool:
    """Whether the trailers name `/Encrypt`, which every encrypted PDF's trailer does

    Trailers and xref stream dictionaries are never encrypted or compressed, so a miss
    rules encryption out without PyMuPDF, while a hit only means PyMuPDF has to look. Only
    the places they live get read: the tail with the last trailer, the xref section that
    `startxref` points at, and the head, where a linearized file keeps its first trailer.
    A file with no `startxref` near its end is broken, so all of it gets searched
    """
    with path.open("rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
        size = len(m)
        tail = m[max(0, size - TRAILER_WINDOW) :]
        if b"/Encrypt" in tail or b"/Encrypt" in m[:HEAD_WINDOW]:
            return True
        at = tail.rfind(b"startxref")
        offset = re.match(rb"startxref\s+(\d+)", tail[at:]) if at >= 0 else None
        if offset is None:
            return m.find(b"/Encrypt") >= 0
        start = int(offset.group(1))
        return b"/Encrypt" in m[start : start + TRAILER_WINDOW]


def _pdf(path: Path) -> Path:
    import pymupdf

    try:
        if not _names_encryption(path):
            return path
    except (OSError, ValueError):
        return path
    with run.LOCK:
        try:
            doc = pymupdf.open(path)
        except Exception:  # noqa: BLE001
            return path
        with doc:
            if not doc.needs_pass:
                return path
            given = password()
            if given is None:
                raise Locked(LOCKED)
            if not doc.authenticate(given):
                raise Locked(WRONG)
            out = run.workdir("meltify-unlock-") / path.name
            try:
                # Keeping the file ID makes every save of the same file identical, so its
                # scanned pages hit the OCR cache from one run to the next
                doc.save(out, encryption=pymupdf.PDF_ENCRYPT_NONE, no_new_id=True)
            except BaseException:
                shutil.rmtree(out.parent, ignore_errors=True)
                raise
    return out


def _office(path: Path) -> Path:
    try:
        import msoffcrypto
        from msoffcrypto.exceptions import InvalidKeyError
        from msoffcrypto.format.ooxml import OOXMLFile
        from msoffcrypto.format.xls97 import Xls97File
    except ImportError:
        if suffix_of(path) in PACKAGES or _office_encrypted(path):
            raise Locked(NEEDS_CRYPTO) from None
        return path

    with path.open("rb") as f:
        try:
            office = msoffcrypto.OfficeFile(f)
            encrypted = office.is_encrypted()
        except Exception:  # noqa: BLE001
            # Mail, HWP and other OLE files msoffcrypto doesn't know aren't Office documents
            return path
        if not encrypted:
            return path
        given = password()
        tries = [given] if given else []
        if isinstance(office, Xls97File):
            tries.append(EXCEL_DEFAULT)
        if not tries:
            raise Locked(LOCKED)
        folder = run.workdir("meltify-unlock-")
        out = folder / path.name
        for secret in tries:
            try:
                if isinstance(office, OOXMLFile):
                    # Without the check, a wrong password decrypts to garbage
                    office.load_key(password=secret, verify_password=True)
                else:
                    office.load_key(password=secret)
                with out.open("wb") as sink:
                    office.decrypt(sink)
                return out
            except InvalidKeyError:
                continue
            except Exception as e:  # noqa: BLE001
                shutil.rmtree(folder, ignore_errors=True)
                raise Locked(cant_decrypt(error_note(e))) from e
            except BaseException:
                shutil.rmtree(folder, ignore_errors=True)
                raise
    shutil.rmtree(folder, ignore_errors=True)
    raise Locked(WRONG if given else LOCKED)


def sealed(path: Path) -> bool:
    """Whether the file is encrypted or DRM-locked, so a render could only show the lock

    unlock already swapped every PDF and Office file it decrypts for a plain copy. What
    is left are files their converters decrypt for themselves, which those converters
    register a check for, and encrypted Office files when msoffcrypto isn't there
    """
    head = b""
    if not path.is_dir():
        try:
            with path.open("rb") as f:
                head = f.read(SEAL_HEAD)
        except OSError:
            return False
    if sealed_natively(path, head):
        return True
    if head.startswith(OLE):
        return _office_encrypted(path)
    if head.startswith(b"PK\x03\x04"):
        return _encrypted_parts(path)
    return False


def _compound(path: Path) -> Any:
    from hwpx.hwp5.cfb import CompoundFile

    try:
        return CompoundFile(path.read_bytes())
    except Exception:  # noqa: BLE001
        return None


def _office_encrypted(path: Path) -> bool:
    compound = _compound(path)
    return compound is not None and _encrypted(compound)


def _encrypted(compound: Any) -> bool:
    """Whether an OLE file holds an encrypted Office document, read from the markers the
    formats set without needing msoffcrypto

    1. Agile and standard encryption wrap the package in EncryptionInfo and EncryptedPackage
    2. Word sets fEncrypted in the FIB at the start of the WordDocument stream
    3. Excel puts a FILEPASS record in the workbook globals, before their first EOF
    4. PowerPoint writes a different token into the Current User atom
    """
    try:
        if compound.has_stream("EncryptionInfo") and compound.has_stream("EncryptedPackage"):
            return True
        if compound.has_stream("WordDocument"):
            fib = compound.read("WordDocument")[:12]
            return len(fib) == 12 and bool(struct.unpack_from("<H", fib, 10)[0] & FIB_ENCRYPTED)
        for name in ("Workbook", "Book"):
            if compound.has_stream(name):
                return _filepass(compound.read(name))
        if compound.has_stream("Current User"):
            atom = compound.read("Current User")
            return len(atom) >= 16 and struct.unpack_from("<I", atom, 12)[0] == PPT_ENCRYPTED
    except Exception:  # noqa: BLE001
        # A stream the reader can't follow isn't proof of a lock
        return False
    return False


def _filepass(biff: bytes) -> bool:
    at = 0
    while at + 4 <= len(biff):
        kind, size = struct.unpack_from("<HH", biff, at)
        if kind == BIFF_FILEPASS:
            return True
        if kind == BIFF_EOF:
            return False
        at += 4 + size
    return False


def _encrypted_parts(path: Path) -> bool:
    """Whether a zip package lists encrypted parts in its manifest, as ODF and HWPX do"""
    import zipfile

    try:
        with zipfile.ZipFile(path) as z:
            if "META-INF/manifest.xml" not in z.namelist():
                return False
            manifest = z.getinfo("META-INF/manifest.xml")
            # The manifest lists each encrypted part, so it stays small
            return manifest.file_size > MAX_MANIFEST_BYTES or b"encryption-data" in z.read(manifest)
    except (OSError, zipfile.BadZipFile):
        return False
