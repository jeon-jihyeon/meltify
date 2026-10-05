"""Archives that nest, hide Korean names, or try to escape, explode and overflow"""

from __future__ import annotations

import io
import stat
import tarfile
import zipfile
from pathlib import Path

TAR_MODES = {
    ".tar": "w",
    ".tgz": "w:gz",
    ".tar.gz": "w:gz",
    ".tbz2": "w:bz2",
    ".tar.bz2": "w:bz2",
    ".txz": "w:xz",
    ".tar.xz": "w:xz",
}


class Cp949Info(zipfile.ZipInfo):
    # Korean Windows Explorer writes cp949 names without the UTF-8 flag 0x800
    def _encodeFilenameFlags(self):
        return self.filename.encode("cp949"), self.flag_bits & ~0x800


def _zip(members: dict[str | zipfile.ZipInfo, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def _pdf(text: str) -> bytes:
    import pymupdf

    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), text, fontsize=12)
    return doc.tobytes()


def nested_zip(root: Path) -> Path:
    """outer.zip → mid.zip → inner.zip → c.txt, plus a fourth level past the depth limit"""
    root.mkdir(parents=True, exist_ok=True)
    deepest = _zip({"too-deep.txt": b"four levels down\n"})
    inner = _zip({"c.txt": b"inner text line\n", "deeper.zip": deepest})
    mid = _zip({"inner.zip": inner, "m.txt": b"middle\n"})
    path = root / "outer.zip"
    path.write_bytes(
        _zip(
            {
                "docs/b.txt": b"hello from b\n",
                "docs/report.pdf": _pdf("Zipped report says ship on Friday"),
                "mid.zip": mid,
            }
        )
    )
    return path


def cp949_zip(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "korean.zip"
    path.write_bytes(_zip({Cp949Info("한글/보고서.txt"): "한국어 본문\n".encode()}))
    return path


def evil_zip(root: Path) -> Path:
    """Traversal, absolute and drive paths next to a symlink and a plain member"""
    root.mkdir(parents=True, exist_ok=True)
    link = zipfile.ZipInfo("link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    path = root / "evil.zip"
    path.write_bytes(
        _zip(
            {
                "../../evil.txt": b"up\n",
                "/abs/evil.txt": b"root\n",
                "C:\\win\\evil.txt": b"drive\n",
                link: b"/etc/passwd",
                "ok.txt": b"fine\n",
            }
        )
    )
    return path


def encrypted_zip(root: Path) -> Path:
    """One member flagged as encrypted next to a plain one"""
    root.mkdir(parents=True, exist_ok=True)
    data = bytearray(_zip({"secret.txt": b"secret\n", "open.txt": b"open\n"}))
    # zipfile can't encrypt, so set bit 0 of the general purpose flags by hand.
    # The local header keeps it at offset 6 and the central directory at offset 8
    local = data.index(b"PK\x03\x04")
    data[local + 6] |= 0x1
    central = data.index(b"PK\x01\x02")
    data[central + 8] |= 0x1
    path = root / "enc.zip"
    path.write_bytes(bytes(data))
    return path


def many_zip(root: Path, count: int = 20_000) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "many.zip"
    with zipfile.ZipFile(path, "w") as z:
        for i in range(count):
            z.writestr(f"f{i}.txt", "")
    return path


def ratio_zip(root: Path, mib: int = 4) -> Path:
    """Zeros compress about 1000:1, far past the 100:1 limit"""
    root.mkdir(parents=True, exist_ok=True)
    path = root / "ratio.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr("zeros.bin", b"\0" * (mib << 20))
        z.writestr("ok.txt", "fine\n")
    return path


def ratio_tgz(root: Path, mib: int = 4) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "ratio.tar.gz"
    with tarfile.open(path, "w:gz", compresslevel=9) as t:
        _add(t, "zeros.bin", b"\0" * (mib << 20))
        _add(t, "after.txt", b"never reached\n")
    return path


def _add(t: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    t.addfile(info, io.BytesIO(data))


def tar_variant(root: Path, suffix: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"bundle{suffix}"
    with tarfile.open(path, TAR_MODES[suffix]) as t:
        _add(t, "docs/b.txt", b"tar line one\ntar line two\n")
    return path


def evil_tar(root: Path) -> Path:
    """Symlink, hard link, FIFO and traversal next to a plain member"""
    root.mkdir(parents=True, exist_ok=True)
    path = root / "evil.tar"
    with tarfile.open(path, "w", format=tarfile.GNU_FORMAT, encoding="cp949") as t:
        _add(t, "docs/ok.txt", b"ok\n")
        _add(t, "../escape.txt", b"x\n")
        _add(t, "한글.txt", "본문\n".encode())
        sym = tarfile.TarInfo("ln")
        sym.type = tarfile.SYMTYPE
        sym.linkname = "/etc/passwd"
        t.addfile(sym)
        hard = tarfile.TarInfo("hard")
        hard.type = tarfile.LNKTYPE
        hard.linkname = "docs/ok.txt"
        t.addfile(hard)
        fifo = tarfile.TarInfo("pipe")
        fifo.type = tarfile.FIFOTYPE
        t.addfile(fifo)
    return path


def seven_zip(root: Path) -> Path:
    """A 7z with a nested zip, written through libarchive since stdlib has no 7z writer"""
    import libarchive

    root.mkdir(parents=True, exist_ok=True)
    path = root / "nested.7z"
    inner = _zip({"c.txt": b"inside 7z then zip\n"})
    with libarchive.file_writer(str(path), "7zip") as a:
        for name, data in {"docs/a.txt": b"seven zip text\n", "docs/inner.zip": inner}.items():
            a.add_file_from_memory(name, len(data), data)
    return path
