from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable, Iterator
from pathlib import Path

# Folders macOS shows as one document. iWork saved them this way before 2013, and still
# does when a document is too big for a single file
BUNDLES = {".pages", ".key", ".numbers"}
# Magic bytes that start a 7z or a RAR archive, which read tells apart by content alone
SEVEN_ZIP_MAGIC = b"7z\xbc\xaf\x27\x1c"
RAR_MAGIC = b"Rar!\x1a\x07"
# Work folder names stay at 80 chars: the URL's tail, an underscore and 8 hash chars
WORK_TAIL = 71


def is_bundle(path: Path) -> bool:
    return path.suffix.lower() in BUNDLES and path.is_dir()


def _walk(root: Path) -> Iterator[Path]:
    for folder, dirs, names in os.walk(root):
        here = Path(folder)
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for d in [d for d in dirs if is_bundle(here / d)]:
            dirs.remove(d)
            yield here / d
        yield from (here / n for n in names if not n.startswith(".") and (here / n).is_file())


def iter_files(paths: Iterable[Path], suffixes: set[str] | None = None) -> Iterator[Path]:
    """Files under each path in sorted order, skipping hidden names

    An iWork bundle folder comes out as one input, like the single file it stands for
    """
    for p in paths:
        if p.is_file() or is_bundle(p):
            candidates = [p]
        elif p.is_dir():
            candidates = sorted(_walk(p))
        else:
            raise FileNotFoundError(p)
        for f in candidates:
            if suffixes is None or f.suffix.lower() in suffixes:
                yield f


def parse_pages(spec: str | None, count: int) -> list[int]:
    """1-based page numbers from a spec like 1,3-5, within the page count"""
    if spec is None:
        return list(range(1, count + 1))
    pages: list[int] = []
    for part in spec.split(","):
        m = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?", part)
        if not m:
            raise ValueError(f"bad page spec {spec!r}, use numbers like 1,3-5")
        lo, hi = int(m[1]), int(m[2] or m[1])
        if lo > hi:
            raise ValueError(f"reversed page range {part.strip()} in {spec!r}")
        if lo < 1 or hi > count:
            raise ValueError(f"pages {part.strip()} outside 1-{count}")
        pages += [n for n in range(lo, hi + 1) if n not in pages]
    return pages


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_name(text: str) -> str:
    """`text` with every run of characters unsafe in a file name turned into one underscore"""
    return re.sub(r"[^\w.\-]+", "_", text)


def work_name(url: str) -> str:
    # The tail keeps the name readable, and the hash keeps URLs with the same tail apart
    return f"{safe_name(url)[-WORK_TAIL:]}_{hashlib.sha256(url.encode()).hexdigest()[:8]}"


def _absolute(path: Path) -> Path:
    # Don't follow links, so a linked file keeps its own name
    return Path(os.path.abspath(path))


def flat_name(path: Path, root: Path | None = None) -> str:
    # One output file per input, with no nested folders
    path = _absolute(path)
    root = _absolute(root) if root else None
    rel = path.relative_to(root) if root and path.is_relative_to(root) else Path(path.name)
    return safe_name("__".join(rel.parts))


def member_name(raw: str | None, index: int) -> str:
    # Senders and archive authors pick these names, so `..`, roots and drive letters must
    # never reach a path. Plain directories stay, since they tell archive members apart
    segments = (raw or "").replace("\\", "/").split("/")
    if segments and re.fullmatch(r"[A-Za-z]:", segments[0]):
        segments = segments[1:]
    kept = [s.strip() for s in segments if s.strip() and set(s.strip()) != {"."}]
    return "/".join(kept) or f"attachment-{index}"


def unique_name(name: str, taken: set[str], identity: str) -> str:
    # Flattening maps a/b.txt and a__b.txt to the same name, and macOS and Windows see
    # Readme.txt and README.txt as one file, so `taken` holds names with case folded.
    # A hash of the full identity keeps the renamed one stable across runs
    if name.casefold() in taken:
        name = f"{name}-{hashlib.sha256(identity.encode()).hexdigest()[:8]}"
    taken.add(name.casefold())
    return name


def fresh(name: str, taken: set[str], index: int, fold: bool = False) -> str:
    # Keep the suffix last, so the renamed copy still picks the same converter. Names bound
    # for disk fold case, since macOS and Windows see Readme.txt and README.txt as one file
    key = str.casefold if fold else str
    while key(name) in taken:
        stem, dot, suffix = name.rpartition(".")
        name = f"{stem}-{index}.{suffix}" if dot and stem else f"{name}-{index}"
        index += 1
    taken.add(key(name))
    return name


def flat_names(paths: Iterable[Path], root: Path | None = None) -> dict[Path, str]:
    taken: set[str] = set()
    return {p: unique_name(flat_name(p, root), taken, str(_absolute(p))) for p in paths}


def common_root(paths: Iterable[Path]) -> Path | None:
    resolved = [_absolute(p) for p in paths]
    if not resolved:
        return None
    # A bundle folder stands for one file, so its parent is the root that names it
    dirs = [p if p.is_dir() and not is_bundle(p) else p.parent for p in resolved]
    root = dirs[0]
    for d in dirs[1:]:
        while not d.is_relative_to(root):
            root = root.parent
    return root
