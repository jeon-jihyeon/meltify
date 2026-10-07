"""Helper binaries that `meltify doctor --install` downloads into the data dir

Every download is pinned to one release and checked against its SHA-256 before anything
is unpacked. LibreOffice hashes come from the Document Foundation's download server, 7-Zip
hashes from the asset digests on its GitHub release
"""

from __future__ import annotations

import hashlib
import io
import os
import platform
import shutil
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from meltify import paths, safe


@dataclass(frozen=True)
class Download:
    url: str
    sha256: str
    size: int

    @property
    def name(self) -> str:
        return self.url.rsplit("/", 1)[-1]


LIBREOFFICE_VERSION = "26.8.1"
_LO = f"https://download.documentfoundation.org/libreoffice/stable/{LIBREOFFICE_VERSION}"
# The Linux build ships as .deb packages, since TDF publishes no AppImage of its own
LIBREOFFICE = {
    ("darwin", "arm64"): Download(
        f"{_LO}/mac/aarch64/LibreOffice_{LIBREOFFICE_VERSION}_MacOS_aarch64.dmg",
        "ca074e0b13571efb30a31fc531c5f95e997030637325e5d5de92d67aefdf0dfc",
        298845477,
    ),
    ("darwin", "x86_64"): Download(
        f"{_LO}/mac/x86_64/LibreOffice_{LIBREOFFICE_VERSION}_MacOS_x86-64.dmg",
        "4c7464313a529e9074400cbcdc894fc40ac4ef9421ded03b16b5a78d12cb155e",
        309092706,
    ),
    ("linux", "x86_64"): Download(
        f"{_LO}/deb/x86_64/LibreOffice_{LIBREOFFICE_VERSION}_Linux_x86-64_deb.tar.gz",
        "30903df3b9f61360d9660cd707de48cd2831469114492a5008ed58a0ac77d044",
        219791834,
    ),
    ("linux", "aarch64"): Download(
        f"{_LO}/deb/aarch64/LibreOffice_{LIBREOFFICE_VERSION}_Linux_aarch64_deb.tar.gz",
        "1b069c20dd237f6decad3ea02cb02f39fdf458b03d09c2a9facb7b9b71e6a27a",
        208175884,
    ),
}

SEVEN_ZIP_VERSION = "26.03"
_7Z = f"https://github.com/ip7z/7zip/releases/download/{SEVEN_ZIP_VERSION}"
# Asset names carry the version without its dot, like 7z2603-mac.tar.xz
_7Z_ASSET = f"{_7Z}/7z{SEVEN_ZIP_VERSION.replace('.', '')}"
_MAC_7Z = Download(
    f"{_7Z_ASSET}-mac.tar.xz",
    "5ca87677072c59f5602e5c49baa27d4694bacd2259b4e507f0094249d4281480",
    1863192,
)
SEVEN_ZIP = {
    # One universal binary for both Mac architectures
    ("darwin", "arm64"): _MAC_7Z,
    ("darwin", "x86_64"): _MAC_7Z,
    ("linux", "x86_64"): Download(
        f"{_7Z_ASSET}-linux-x64.tar.xz",
        "dc99eff5008f1ab79bd7084c68513701547a808a89502bf4133683535ab3c695",
        1575072,
    ),
    ("linux", "aarch64"): Download(
        f"{_7Z_ASSET}-linux-arm64.tar.xz",
        "2389ba20e4d8295e8709c20b6263b69bd1ec4972fe38a04ad7a1badbf595b996",
        1328620,
    ),
}
# The binary, and the license 7-Zip's terms ask to ship along with it
SEVEN_ZIP_FILES = ("7zz", "License.txt", "readme.txt")
DOWNLOAD_TIMEOUT = 60
DOWNLOAD_ATTEMPTS = 3
HDIUTIL_TIMEOUT = 300


def find(name: str, *installed: str) -> str | None:
    """A downloaded copy first, then the system PATH

    `installed` lists paths inside the tools dir to try, like `libreoffice/program/soffice`
    """
    for rel in installed or (name,):
        path = paths.tools_dir() / rel
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return shutil.which(name)


def seven_zip() -> str | None:
    return find("7zz", "7zip/7zz") or shutil.which("7z") or shutil.which("7za")


def platform_key() -> tuple[str, str]:
    machine = platform.machine().lower()
    machine = {"amd64": "x86_64", "x64": "x86_64"}.get(machine, machine)
    if sys.platform.startswith("linux") and machine == "arm64":
        machine = "aarch64"
    return ("linux" if sys.platform.startswith("linux") else sys.platform, machine)


def pinned(table: dict[tuple[str, str], Download], what: str) -> Download:
    key = platform_key()
    if key not in table:
        raise RuntimeError(f"no {what} download for {key[0]} {key[1]}, install it yourself")
    return table[key]


def _get(url: str, target: Path, limit: int) -> tuple[int, str]:
    """Size and SHA-256 of the download, stopping once it runs past `limit` bytes"""
    import httpx

    digest = hashlib.sha256()
    size = 0
    with (
        httpx.stream("GET", url, follow_redirects=True, timeout=DOWNLOAD_TIMEOUT) as r,
        target.open("wb") as out,
    ):
        r.raise_for_status()
        for chunk in r.iter_bytes(1 << 20):
            size += len(chunk)
            if size > limit:
                # A mirror sending more than the pinned file can't pass the hash check, and
                # it shouldn't fill the disk on the way there
                raise RuntimeError(f"{target.name} is larger than its pinned {limit} bytes")
            digest.update(chunk)
            out.write(chunk)
    return size, digest.hexdigest()


def fetch(item: Download, folder: Path) -> Path:
    """Download into `folder`, refusing a file whose size or SHA-256 isn't the pinned one"""
    import httpx

    folder.mkdir(parents=True, exist_ok=True)
    target = folder / item.name
    for attempt in range(DOWNLOAD_ATTEMPTS):
        try:
            size, digest = _get(item.url, target, item.size)
            break
        except httpx.TransportError as e:
            # The LibreOffice URL redirects to a mirror, and a stalled one is worth a retry
            if attempt == DOWNLOAD_ATTEMPTS - 1:
                target.unlink(missing_ok=True)
                raise RuntimeError(f"couldn't download {item.name}: {e}") from e
        except httpx.HTTPError as e:
            # A 404 or a bad redirect won't change on retry, and callers only expect
            # RuntimeError from a download that didn't work
            target.unlink(missing_ok=True)
            raise RuntimeError(f"couldn't download {item.name}: {e}") from e
        except BaseException:
            target.unlink(missing_ok=True)
            raise
    if size != item.size or digest != item.sha256:
        target.unlink()
        raise RuntimeError(
            f"{item.name} doesn't match its pinned hash ({size} bytes, sha256 "
            f"{digest[:12]}...), so it wasn't installed"
        )
    return target


def _replace(staged: Path, final: Path) -> Path:
    if final.exists():
        shutil.rmtree(final)
    staged.rename(final)
    return final


def _attach(dmg: Path, mount: Path) -> None:
    mount.mkdir()
    # Some DMGs show a license first, which hdiutil reads from stdin
    proc = safe.run(
        ["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen",
         "-mountpoint", str(mount), str(dmg)],
        input="Y\n", timeout=HDIUTIL_TIMEOUT, check=False,
    )  # fmt: skip
    if proc.returncode != 0:
        raise RuntimeError(f"hdiutil couldn't open {dmg.name}: {proc.stderr.strip()[-200:]}")


def _mac_libreoffice(dmg: Path, root: Path, work: Path) -> Path:
    mount = work / "mount"
    _attach(dmg, mount)
    try:
        staged = root / "LibreOffice.app.partial"
        shutil.rmtree(staged, ignore_errors=True)
        # ditto keeps the code signature and extended attributes the app needs to launch
        safe.run(["ditto", str(mount / "LibreOffice.app"), str(staged)], timeout=HDIUTIL_TIMEOUT)
    finally:
        safe.run(["hdiutil", "detach", "-force", str(mount)], timeout=HDIUTIL_TIMEOUT, check=False)
    return _replace(staged, root / "LibreOffice.app") / "Contents" / "MacOS" / "soffice"


def _deb_data(deb: IO[bytes]) -> IO[bytes] | None:
    """The data.tar member of a .deb, which is a plain `ar` archive"""
    if deb.read(8) != b"!<arch>\n":
        return None
    while header := deb.read(60):
        if len(header) < 60:
            return None
        name = header[:16].decode("ascii", "replace").strip().rstrip("/")
        size = int(header[48:58].decode("ascii").strip())
        body = deb.read(size)
        if size % 2:
            deb.read(1)
        if name.startswith("data.tar"):
            return io.BytesIO(body)
    return None


def _under_opt(member: tarfile.TarInfo, dest: str) -> tarfile.TarInfo | None:
    # Only the install tree, through the stdlib's checks against links and paths that escape
    if not member.name.lstrip("./").startswith("opt/"):
        return None
    try:
        return tarfile.data_filter(member, dest)
    except tarfile.FilterError:
        return None


def _linux_libreoffice(tarball: Path, root: Path, work: Path) -> Path:
    staging = work / "unpacked"
    with tarfile.open(tarball) as outer:
        for member in outer:
            if not (member.isfile() and member.name.endswith(".deb")):
                continue
            deb = outer.extractfile(member)
            data = None if deb is None else _deb_data(deb)
            if data is None:
                continue
            with tarfile.open(fileobj=data, mode="r:*") as inner:
                inner.extractall(staging, filter=_under_opt)
    program = next(staging.glob("opt/libreoffice*/program/soffice"), None)
    if program is None:
        raise RuntimeError(f"{tarball.name} held no soffice")
    return _replace(program.parent.parent, root / "libreoffice") / "program" / "soffice"


def install_libreoffice(root: Path) -> Path:
    """Portable LibreOffice in `root`, returning its soffice"""
    item = pinned(LIBREOFFICE, "LibreOffice")
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="meltify-libreoffice-", dir=root) as tmp:
        got = fetch(item, Path(tmp))
        if item.name.endswith(".dmg"):
            return _mac_libreoffice(got, root, Path(tmp))
        return _linux_libreoffice(got, root, Path(tmp))


def install_seven_zip(root: Path) -> Path:
    """7-Zip's `7zz` with its license in `root/7zip`, returning the binary"""
    item = pinned(SEVEN_ZIP, "7-Zip")
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="meltify-7zip-", dir=root) as tmp:
        got = fetch(item, Path(tmp))
        staged = Path(tmp) / "7zip"
        staged.mkdir()
        with tarfile.open(got) as tar:
            # Only the named files, written by name, so no member path reaches the disk
            for member in tar:
                name = member.name.lstrip("./")
                if member.isfile() and name in SEVEN_ZIP_FILES:
                    src = tar.extractfile(member)
                    if src is not None:
                        (staged / name).write_bytes(src.read())
        binary = staged / "7zz"
        if not binary.is_file():
            raise RuntimeError(f"{item.name} held no 7zz")
        binary.chmod(0o755)
        return _replace(staged, root / "7zip") / "7zz"
