from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable, Iterator
from pathlib import Path


def iter_files(paths: Iterable[Path], suffixes: set[str] | None = None) -> Iterator[Path]:
    """Files under each path in sorted order, skipping hidden names"""
    for p in paths:
        if p.is_file():
            candidates = [p]
        elif p.is_dir():
            candidates = sorted(
                f
                for f in p.rglob("*")
                if f.is_file() and not any(part.startswith(".") for part in f.relative_to(p).parts)
            )
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


def _absolute(path: Path) -> Path:
    # Don't follow links, so a linked file keeps its own name
    return Path(os.path.abspath(path))


def flat_name(path: Path, root: Path | None = None) -> str:
    # One output file per input, with no nested folders
    path = _absolute(path)
    root = _absolute(root) if root else None
    rel = path.relative_to(root) if root and path.is_relative_to(root) else Path(path.name)
    return re.sub(r"[^\w.\-]+", "_", "__".join(rel.parts))


def unique_name(name: str, taken: set[str], identity: str) -> str:
    # Flattening maps a/b.txt and a__b.txt to the same name.
    # A hash of the full identity keeps the renamed one stable across runs
    if name in taken:
        name = f"{name}-{hashlib.sha256(identity.encode()).hexdigest()[:8]}"
    taken.add(name)
    return name


def flat_names(paths: Iterable[Path], root: Path | None = None) -> dict[Path, str]:
    taken: set[str] = set()
    return {p: unique_name(flat_name(p, root), taken, str(_absolute(p))) for p in paths}


def common_root(paths: Iterable[Path]) -> Path | None:
    resolved = [_absolute(p) for p in paths]
    if not resolved:
        return None
    dirs = [p if p.is_dir() else p.parent for p in resolved]
    root = dirs[0]
    for d in dirs[1:]:
        while not d.is_relative_to(root):
            root = root.parent
    return root
