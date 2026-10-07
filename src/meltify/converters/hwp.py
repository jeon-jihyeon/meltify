from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import re
import warnings
import zipfile
import zlib
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

from meltify import passwords
from meltify.converters import Block, Converted, RecognizeJob
from meltify.converters.limits import MAX_PART_BYTES, EmbedBudget
from meltify.converters.tables import escape_cell
from meltify.converters.text import numbered_lines
from meltify.evidence import Src
from meltify.passwords import NEEDS_CRYPTO, NEEDS_DRM, Locked

PICTURES = {"jpg", "jpeg", "png", "bmp", "gif", "tif", "tiff", "webp"}
# Vector pictures, read from their text records and drawn for OCR the way Office ones are
METAFILES = {"wmf", "emf"}
# BinData Hancom stores for embedded OLE objects, like a chart or a spreadsheet
OBJECTS = {"ole"}
OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

# Fasoo and SoftCamp wrap the whole file, MarkAny names itself near the start
DRM_MAGIC = (b"\x9b DRMONE", b"SCDSA")
DRM_NAMES = (b"MarkAny", b"MARKANY")
# How far into the file a DRM wrapper names itself
SEAL_HEAD = 1024
# FileHeader flags for a body sealed to a DRM client or a certificate
DRM_FLAGS = ("drm", "cert_encrypted", "cert_drm")
# python-hwpx error codes for a body it can't read without a key, as needs lines
SEALED = {
    "hwp5-password": passwords.LOCKED,
    "hwp5-distribution": passwords.DISTRIBUTION,
    "hwp5-drm": NEEDS_DRM,
}
# Password streams decrypt one bit per AES call, about 3 seconds per MB in Python,
# so pictures share a budget while the body text is always read
PICTURE_BUDGET = 4 * 1024 * 1024
OVERSIZED = "hwpx part {} over the size limit"
ENCRYPTION_DATA = re.compile(r"<(\w+:)?encryption-data\b.*?</(\w+:)?encryption-data>", re.S)
DISTRIBUTE_DOC_DATA = 28
DOCUMENT_PROPERTIES = 16


def table_markdown(table: Any) -> str | None:
    # A merged cell shows its text once at its anchor and stays blank where it spans
    rows = [[""] * table.column_count for _ in range(table.row_count)]
    for pos in table.iter_grid():
        if (pos.row, pos.column) == tuple(pos.anchor):
            rows[pos.row][pos.column] = escape_cell((pos.cell.text or "").strip())
    if not any(any(r) for r in rows):
        return None
    lines = ["| " + " | ".join(rows[0]) + " |", "|---" * table.column_count + "|"]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines)


def _section(section: Any, src: Src, out: Converted) -> None:
    # A cite line is the paragraph's index in its section, so empty paragraphs still count
    lines: list[tuple[int, str]] = []
    width = len(str(len(section.paragraphs)))

    def flush() -> None:
        out.blocks.extend(numbered_lines(lines, src, width))
        lines.clear()

    for n, para in enumerate(section.paragraphs, start=1):
        if text := para.text.strip():
            lines.append((n, text))
        tables = [md for t in para.tables if t.row_count and (md := table_markdown(t))]
        if tables:
            flush()
            out.blocks += [Block(replace(src, line=n), md) for md in tables]
    flush()


def _pictures(doc: Any, src: Src, out: Converted) -> None:
    from meltify.converters.embeds import Embeds

    items = {i.item_id: i for i in doc.media.images}
    seen: set[str] = set()
    counts: dict[int, int] = {}
    embeds = Embeds()
    for ref in doc.media.picture_references():
        item = items.get(ref.binary_item_id_ref)
        # The same picture drawn twice would only OCR into a duplicate block
        if item is None or item.item_id in seen:
            continue
        seen.add(item.item_id)
        section = ref.section_index + 1
        counts[section] = counts.get(section, 0) + 1
        job_src = replace(src, section=section, img=counts[section])
        kind = item.format.lower()
        if kind in OBJECTS:
            embeds.skipped["embedded object"] += 1
            continue
        data = doc.package.get_part(item.href)
        if kind in PICTURES:
            out.jobs.append(RecognizeJob("image", job_src, data=data))
        else:
            # WMF and EMF give their text records and drawings, and anything else becomes a need
            embeds.picture(job_src, data, f"{item.item_id}.{kind}")
    embeds.into(out)


def _rand(seed: int) -> Iterator[int]:
    # The MSVC rand() Hancom seeds from the distribution record
    while True:
        seed = (seed * 214013 + 2531011) & 0xFFFFFFFF
        yield (seed >> 16) & 0x7FFF


def _view_text(raw: bytes) -> bytes:
    """A distribution section: a 256-byte record holding the key, then AES-128-ECB

    The record is XORed with runs of rand() bytes seeded from its first 4 bytes,
    and the key sits at 4 plus the low nibble of its first byte
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    word = int.from_bytes(raw[:4], "little")
    size, at = word >> 20, 4
    if size == 0xFFF:
        size, at = int.from_bytes(raw[4:8], "little"), 8
    if word & 0x3FF != DISTRIBUTE_DOC_DATA or size < 256:
        raise ValueError("distribution section without its key record")
    record = bytearray(raw[at : at + 256])
    rand = _rand(int.from_bytes(record[:4], "little"))
    run = mask = 0
    for i in range(256):
        if run == 0:
            mask, run = next(rand) & 0xFF, (next(rand) & 0xF) + 1
        if i >= 4:
            record[i] ^= mask
        run -= 1
    key = bytes(record[4 + (record[0] & 0xF) :][:16])
    body = raw[at + size :]
    body = body[: len(body) - len(body) % 16]
    plain = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    return plain.update(body) + plain.finalize()


def _password_key(secret: str) -> bytes:
    # SHA-1 over the password bytes, each one led by its predecessor rotated left by 1
    prev, mixed = 0xEC, bytearray()
    for b in secret.encode("utf-8"):
        mixed += bytes((((prev << 1) | (prev >> 7)) & 0xFF, b))
        prev = b
    return hashlib.sha1(mixed).digest()[:16]


def _cfb1(key: bytes, data: bytes) -> bytes:
    """AES-128 in 1-bit CFB from a zero IV, which cryptography has no mode for"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    encrypt = Cipher(algorithms.AES(key), modes.ECB()).encryptor().update
    register, mask = 0, (1 << 128) - 1
    out = bytearray(len(data))
    for i, byte in enumerate(data):
        plain = 0
        for shift in range(7, -1, -1):
            bit = (byte >> shift) & 1
            plain |= ((encrypt(register.to_bytes(16, "big"))[0] >> 7) ^ bit) << shift
            register = ((register << 1) | bit) & mask
        out[i] = plain
    return bytes(out)


def _password_streams(streams: dict[str, bytes], header: Any, secret: str, out: Converted) -> None:
    """Decrypt DocInfo, the body and pictures of an EncryptVersion 4 document in place"""
    from hwpx.hwp5.docinfo import BIN_LINK, decode_docinfo
    from hwpx.hwp5.records import inflate, parse_records

    key = _password_key(secret)
    docinfo = _cfb1(key, streams["DocInfo"])
    # A wrong key leaves noise, which fails to inflate or doesn't start like DocInfo
    try:
        records = parse_records(inflate(docinfo, "DocInfo") if header.compressed else docinfo, "")
        ok = bool(records.roots) and records.roots[0].tag == DOCUMENT_PROPERTIES
    except Exception:  # noqa: BLE001
        ok = False
    if not ok:
        raise Locked(passwords.WRONG)
    streams["DocInfo"] = docinfo
    # python-hwpx inflates what the item record says is compressed, and the rest as stored
    stored = {
        f"BinData/{item.stream_name}"
        for item in decode_docinfo(records).bin_data
        if item.kind != BIN_LINK
        and not (item.compression == 1 or (item.compression == 0 and header.compressed))
    }
    budget, skipped = PICTURE_BUDGET, 0
    for name in list(streams):
        if name.startswith("BodyText/"):
            streams[name] = _cfb1(key, streams[name])
        elif name.startswith("BinData/"):
            data = streams.pop(name)
            kind = name.rsplit(".", 1)[-1].lower()
            if kind in PICTURES | METAFILES and len(data) <= budget:
                budget -= len(data)
                plain = _cfb1(key, data)
                streams[name] = _inflated(plain) if name in stored else plain
            else:
                skipped += 1
        elif name.startswith("Scripts/"):
            # Document macros, never melted, and not worth the decrypt time
            del streams[name]
    if skipped:
        out.needs.append(f"hwp skipped {skipped} encrypted embedded files")


def _inflated(data: bytes) -> bytes:
    """A picture Hancom deflated before encrypting, though its record says stored

    Password documents write GIFs that way, so python-hwpx would hand OCR the deflate
    stream. Bytes after the stream end are padding. Data that isn't a complete raw deflate
    stream stays as it is
    """
    from hwpx.hwp5.records import MAX_INFLATED_BYTES

    d = zlib.decompressobj(-15)
    try:
        out = d.decompress(data, MAX_INFLATED_BYTES)
    except zlib.error:
        return data
    return out if d.eof and out else data


def _unlock_hwp(data: bytes, out: Converted) -> bytes:
    """A plain copy of a distribution or password HWP that python-hwpx can open"""
    from hwpx.hwp5.cfb import CompoundFile, build_compound_file, read_all_streams, storage_times
    from hwpx.hwp5.fileheader import FLAG_BITS, parse_file_header

    compound = CompoundFile(data)
    if not compound.has_stream("FileHeader"):
        return data
    header = parse_file_header(compound.read("FileHeader"))
    if any(header.has(f) for f in DRM_FLAGS):
        raise Locked(NEEDS_DRM)
    sealed = [f for f in ("password", "distribution") if header.has(f)]
    if not sealed:
        return data
    secret = passwords.password()
    if "password" in sealed and secret is None:
        raise Locked(passwords.LOCKED)
    if importlib.util.find_spec("cryptography") is None:
        raise Locked(NEEDS_CRYPTO)
    if "password" in sealed and header.encrypt_version != 4:
        raise Locked(f"hwp password scheme {header.encrypt_version} not supported")
    streams = dict(read_all_streams(compound))
    if "password" in sealed:
        _password_streams(streams, header, secret or "", out)
    if "distribution" in sealed:
        # The readable text lives in ViewText, BodyText only holds a stand-in notice
        for name in [n for n in streams if n.startswith("ViewText/")]:
            streams["BodyText/" + name.split("/", 1)[1]] = _view_text(streams.pop(name))
        streams = {n: d for n, d in streams.items() if not n.startswith("Scripts/")}
    flags = header.flags
    for f in sealed:
        flags &= ~(1 << FLAG_BITS[f])
    streams["FileHeader"] = replace(header, flags=flags).to_bytes()
    return build_compound_file(
        streams.items(), storage_times=storage_times(compound), root_modified=compound.root.modified
    )


def _unlock_hwpx(data: bytes, out: Converted) -> bytes | None:
    """A plain copy of a password HWPX, which uses ODF package encryption

    Each listed part is deflated then AES-256-CBC encrypted, keyed by PBKDF2 over the
    SHA-256 of the password. Hancom writes HMAC-SHA1 and newer writers HMAC-SHA256,
    so both are tried and the SHA-256 of the first 1K of plaintext picks the right one.
    None when a part is too big to rebuild in memory, listed in needs
    """
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        if "META-INF/manifest.xml" not in z.namelist():
            return data
        if z.getinfo("META-INF/manifest.xml").file_size > MAX_PART_BYTES:
            out.needs.append(OVERSIZED.format("META-INF/manifest.xml"))
            return None
        manifest = z.read("META-INF/manifest.xml").decode("utf-8", "replace")
        sealed = _sealed_parts(manifest)
        if not sealed:
            return data
        secret = passwords.password()
        if secret is None:
            raise Locked(passwords.LOCKED)
        if importlib.util.find_spec("cryptography") is None:
            raise Locked(NEEDS_CRYPTO)
        try:
            return _rebuilt(z, manifest, sealed, secret, EmbedBudget(len(data)))
        except Oversized as e:
            out.needs.append(OVERSIZED.format(e))
            return None


def _rebuilt(
    z: zipfile.ZipFile,
    manifest: str,
    sealed: dict[str, dict[str, str]],
    secret: str,
    budget: EmbedBudget,
) -> bytes:
    """The package with its sealed parts decrypted and the manifest left without them

    The rebuilt package sits in memory, so each part has a size limit and all of them
    share one budget. Raises Oversized with the part's name when one doesn't fit
    """
    plain = io.BytesIO()
    with zipfile.ZipFile(plain, "w", zipfile.ZIP_DEFLATED) as w:
        for info in z.infolist():
            if info.file_size > MAX_PART_BYTES or not budget.spend(info.file_size):
                raise Oversized(info.filename)
            body = z.read(info)
            if info.filename == "META-INF/manifest.xml":
                body = ENCRYPTION_DATA.sub("", manifest).encode()
            elif info.filename in sealed:
                # zlib reads a max_length of 0 as no limit at all
                limit = max(1, min(MAX_PART_BYTES, budget.left))
                try:
                    body = _open_part(body, secret, sealed[info.filename], limit)
                except Oversized:
                    raise Oversized(info.filename) from None
                budget.spend(len(body))
            kind = zipfile.ZIP_STORED if info.filename == "mimetype" else zipfile.ZIP_DEFLATED
            w.writestr(info.filename, body, compress_type=kind)
    return plain.getvalue()


def _sealed_parts(manifest: str) -> dict[str, dict[str, str]]:
    # Each part's attributes and its nested encryption tags run up to the next entry,
    # and every attribute name used here is unique within one entry
    found = {}
    for body in re.split(r"<(?:\w+:)?file-entry\b", manifest)[1:]:
        if "encryption-data" not in body:
            continue
        attrs = dict(re.findall(r"(?:\w+:)?([\w-]+)=\"([^\"]*)\"", body))
        if "full-path" in attrs:
            found[attrs["full-path"]] = attrs
    return found


class Oversized(ValueError):
    """A part too big to rebuild in memory, its name the message once it's known"""


def _open_part(body: bytes, secret: str, attrs: dict[str, str], limit: int) -> bytes:
    """The part's plaintext, raising Locked for a wrong password"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    salt = base64.b64decode(attrs.get("salt", ""))
    iv = base64.b64decode(attrs.get("initialisation-vector", ""))
    check = base64.b64decode(attrs.get("checksum", ""))
    rounds = int(attrs.get("iteration-count", "0"))
    if "aes256-cbc" not in attrs.get("algorithm-name", "") or not 0 < rounds <= 1_000_000:
        raise ValueError("unsupported HWPX encryption")
    if len(iv) != 16 or len(body) % 16:
        raise Locked(passwords.WRONG)
    start = hashlib.sha256(secret.encode("utf-8")).digest()
    for prf in ("sha1", "sha256"):
        key = hashlib.pbkdf2_hmac(prf, start, salt, rounds, 32)
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        inflate = zlib.decompressobj(-15)
        try:
            text = inflate.decompress(cipher.update(body) + cipher.finalize(), limit)
        except zlib.error:
            continue
        if hashlib.sha256(text[:1024]).digest() == check:
            # Stopped at the limit with input left, so the whole part is bigger
            if inflate.unconsumed_tail:
                raise Oversized
            return text
    raise Locked(passwords.WRONG)


def _drm(head: bytes) -> bool:
    return head.startswith(DRM_MAGIC) or (
        not head.startswith((OLE, b"PK\x03\x04")) and any(n in head for n in DRM_NAMES)
    )


def sealed(path: Path, head: bytes) -> bool:
    """Whether DRM or a password seals an HWP, read from its wrapper or its FileHeader flags

    A password HWPX lists its encrypted parts in the manifest, which unlock reads itself
    """
    if _drm(head):
        return True
    if not head.startswith(OLE):
        return False
    from hwpx.hwp5.cfb import CompoundFile
    from hwpx.hwp5.fileheader import parse_file_header

    try:
        compound = CompoundFile(path.read_bytes())
        if not compound.has_stream("FileHeader"):
            return False
        header = parse_file_header(compound.read("FileHeader"))
    except Exception:  # noqa: BLE001
        # A container the reader can't follow is the converter's to report
        return False
    return any(header.has(f) for f in ("password", "distribution", *DRM_FLAGS))


def _unlock(path: Path, out: Converted) -> bytes | None:
    """The bytes python-hwpx opens, raising Locked for a file that stays shut

    None when the file is too big to decrypt, listed in needs
    """
    data = path.read_bytes()
    if _drm(data[:SEAL_HEAD]):
        raise Locked(NEEDS_DRM)
    if data.startswith(OLE):
        return _unlock_hwp(data, out)
    if data.startswith(b"PK\x03\x04"):
        return _unlock_hwpx(data, out)
    return data


def convert(path: Path, src: Src) -> Converted:
    from hwpx import HwpxDocument
    from hwpx.hwp5.errors import Hwp5Error

    out = Converted("hwp")
    data = _unlock(path, out)
    if data is None:
        return out
    try:
        # Conversion gaps are reported through conversion_report, not as warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            doc = HwpxDocument.open(data)
    except Hwp5Error as e:
        # A lock the header checks above missed. Locked keeps read from rendering the
        # file, since a render would show only the lock
        if (need := SEALED.get(e.code)) is not None:
            raise passwords.Locked(need) from e
        raise
    try:
        for i, section in enumerate(doc.sections, start=1):
            _section(section, replace(src, section=i), out)
        _pictures(doc, src, out)
        report = doc.conversion_report
        if report and report.unconverted:
            kinds = ", ".join(f"{k} x{n}" for k, n in sorted(report.unconverted.items()))
            out.needs.append(f"hwp skipped {kinds}")
    finally:
        doc.close()
    return out
