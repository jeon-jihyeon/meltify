"""The files of an iWork package, and the key that opens a password-locked one"""

from __future__ import annotations

import functools
import hashlib
import importlib.util
import io
import struct
import zipfile
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from meltify import passwords
from meltify.converters import Converted, iwa
from meltify.converters.limits import MAX_MEMBER_BYTES, MAX_RATIO, human_bytes, inflates
from meltify.needs import not_read
from meltify.passwords import NEEDS_CRYPTO, Locked

# Apple's PBKDF2 count is 100k, so anything far past it is a file built to stall us
MAX_ITERATIONS = 10_000_000
# Files a locked bundle leaves readable, so the app can show the hint and metadata
PLAIN = (".iwph", ".iwpv2", "Metadata/")


@dataclass
class Bundle:
    """The files of a package, whether a folder, a zip or a zip holding a flattened one"""

    names: list[str]
    size: Callable[[str], int]
    raw: Callable[[str], bytes]
    key: bytes | None = None
    # A zip member's compressed size, None for a file in a bundle folder
    packed: Callable[[str], int | None] = lambda name: None
    # Symbolic links in a bundle folder, which are never followed
    links: list[str] = field(default_factory=list)

    def read(self, name: str) -> bytes:
        if self.size(name) > MAX_MEMBER_BYTES:
            raise ValueError(f"{name} is over the {human_bytes(MAX_MEMBER_BYTES)} limit")
        data = self.raw(name)
        if self.key is None or name.startswith(PLAIN):
            return data
        return _decrypt(self.key, data) or data

    def has(self, name: str) -> bool:
        return name in self.names


def _flat_prefix(names: list[str]) -> str:
    # Keynote 2018 flattens the package into one folder and zips its index again
    if any(n.startswith(("Index/", "Index.zip", "index.")) for n in names):
        return ""
    for n in sorted(names):
        if n.endswith("/Index.zip") and n.count("/") == 1:
            return n[: -len("Index.zip")]
    return ""


def _folder(path: Path) -> Bundle:
    """A bundle folder's files, leaving out links, which could point anywhere on disk"""
    files: dict[str, Path] = {}
    links: list[str] = []
    for f in sorted(path.rglob("*")):
        name = f.relative_to(path).as_posix()
        if f.is_symlink():
            links.append(name)
        elif f.is_file():
            files[name] = f
    return Bundle(
        list(files),
        lambda n: files[n].stat().st_size,
        lambda n: files[n].read_bytes(),
        links=links,
    )


def open_bundle(path: Path, z: zipfile.ZipFile | None) -> Bundle:
    if z is None:
        return _folder(path)
    infos = {i.filename: i for i in z.infolist() if not i.is_dir()}
    prefix = _flat_prefix(list(infos))
    names = [n[len(prefix) :] for n in infos if n.startswith(prefix)]
    return Bundle(
        names,
        lambda n: infos[prefix + n].file_size,
        lambda n: z.read(infos[prefix + n]),
        packed=lambda n: infos[prefix + n].compress_size,
    )


Member = tuple[int, int | None, Callable[[], bytes]]


def index_files(bundle: Bundle, out: Converted) -> Iterator[bytes]:
    """The Index/*.iwa files one at a time, so each can go once it's decompressed"""
    if not bundle.has("Index.zip"):
        names = [n for n in bundle.names if n.startswith("Index/") and n.endswith(".iwa")]
        members = [
            (bundle.size(n), bundle.packed(n), functools.partial(bundle.read, n)) for n in names
        ]
        yield from _capped(members, None, out)
        return
    # Index files are Snappy data already, so one that deflates past the ratio is built to bloat
    if inflates(bundle.size("Index.zip"), bundle.packed("Index.zip")):
        out.needs.append(f"iwork Index.zip not read (expands over {MAX_RATIO}:1)")
        return
    with zipfile.ZipFile(io.BytesIO(bundle.read("Index.zip"))) as inner:
        infos = [i for i in inner.infolist() if i.filename.endswith(".iwa")]
        members = [(i.file_size, i.compress_size, functools.partial(inner.read, i)) for i in infos]
        yield from _capped(members, bundle.key, out)


def _capped(members: list[Member], key: bytes | None, out: Converted) -> Iterator[bytes]:
    """Index files within one total for the document, decrypted with `key` when given

    Snappy output is never much smaller than its input, so files past the cap on
    decompressed streams couldn't be decompressed anyway
    """
    left = iwa.MAX_STREAM_BYTES
    skipped: Counter[str] = Counter()
    for size, packed, read in members:
        if inflates(size, packed):
            skipped[f"expands over {MAX_RATIO}:1"] += 1
            continue
        if size > left:
            skipped[f"over the {human_bytes(iwa.MAX_STREAM_BYTES)} index total"] += 1
            continue
        left -= size
        data = read()
        yield data if key is None else _decrypt(key, data) or data
    for why, n in skipped.items():
        out.needs.append(not_read(n, "iwork index file", why))


def _decrypt(key: bytes, data: bytes) -> bytes | None:
    """One file of a locked bundle: an IV, AES-128-CBC with PKCS7, then 20 trailing bytes

    The first plaintext block is filler. None when the bytes don't decrypt, which is
    how a file the bundle left in the clear looks
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    body = data[16:-20]
    if len(data) < 52 or len(body) % 16 or iwa.is_iwa(data):
        return None
    plain = Cipher(algorithms.AES(key), modes.CBC(data[:16])).decryptor()
    out = plain.update(body) + plain.finalize()
    pad = out[-1]
    if not 1 <= pad <= 16 or out[-pad:] != bytes([pad]) * pad or len(out) - pad < 16:
        return None
    return out[16:-pad]


def unlock(bundle: Bundle) -> None:
    """Derive the key from the .iwpv2 verifier, raising Locked when it can't be

    The verifier is version and format words, a PBKDF2-SHA1 count, a salt, an IV and
    64 bytes whose second half is the SHA-256 of the first half once decrypted
    """
    secret = passwords.password()
    if secret is None:
        raise Locked(passwords.LOCKED)
    if importlib.util.find_spec("cryptography") is None:
        raise Locked(NEEDS_CRYPTO)
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    verifier = bundle.read(".iwpv2")
    if len(verifier) != 104:
        raise ValueError("unrecognized .iwpv2 password verifier")
    version, kind, rounds = struct.unpack_from("<HHI", verifier)
    if (version, kind) != (2, 1) or not 0 < rounds <= MAX_ITERATIONS:
        raise ValueError(f"unsupported iWork encryption {version}.{kind}")
    salt, iv, check = verifier[8:24], verifier[24:40], verifier[40:]
    key = hashlib.pbkdf2_hmac("sha1", secret.encode("utf-8"), salt, rounds, 16)
    plain = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor().update(check)
    if hashlib.sha256(plain[:32]).digest() != plain[32:]:
        raise Locked(passwords.WRONG)
    bundle.key = key
