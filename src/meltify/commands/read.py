from __future__ import annotations

import argparse
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from typing import Any

from meltify import __version__
from meltify.evidence import Envelope, Src, finding
from meltify.files import common_root, flat_name, flat_names, iter_files, unique_name
from meltify.output import append_jsonl
from meltify.safe import MissingTool

NAME = "read"
HELP = (
    "melt files and folders of any format into cited markdown"
    " and list what still needs OCR, media or hidden text checks"
)
COLUMNS = ["kind", "chars", "needs", "out", "cite"]

# How many levels of nested attachments to follow
MAX_DEPTH = 2


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("paths", nargs="+", type=Path, help="files or folders")
    p.add_argument("--jobs", type=int, help="parallel workers for non-PDF files")


def attachment_name(raw: str | None, index: int) -> str:
    # Mail senders pick these names, so directories and dot names must never reach a path
    name = PurePosixPath((raw or "").replace("\\", "/")).name.strip()
    if not name or set(name) == {"."}:
        return f"attachment-{index}"
    return name


class Reader:
    def __init__(self, out_dir: Path, names: dict[Path, str]) -> None:
        self.out_dir = out_dir
        self.names = names
        self.taken = set(names.values())
        # PyMuPDF isn't thread-safe, and attachments can be PDFs too
        self.pdf_lock = threading.Lock()
        self.name_lock = threading.Lock()

    def one(
        self, path: Path, src: Src, name: str | None = None, depth: int = 0
    ) -> list[dict[str, Any]]:
        from meltify.converters import pick

        kind, convert = pick(path)
        row = finding(src, kind=kind, chars=0, needs=[], hidden=0, out=None)
        try:
            if kind == "pdf":
                with self.pdf_lock:
                    converted = convert(path, src)
            else:
                converted = convert(path, src)
        except MissingTool as e:
            return [{**row, "error": str(e), "hint": e.hint, "needs": [e.name]}]
        except Exception as e:  # noqa: BLE001
            # One unreadable file shouldn't stop the rest of the folder
            return [{**row, "error": f"{type(e).__name__}: {e}"[:300]}]

        name = name or self.names[path]
        target = self.out_dir / f"{name}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(converted.markdown(src.cite()), encoding="utf-8")
        row.update(
            kind=converted.kind,
            chars=converted.chars,
            needs=list(converted.needs),
            hidden=converted.hidden,
            out=str(target),
        )
        rows = [row]
        if depth >= MAX_DEPTH:
            if converted.attachments:
                row["needs"].append(
                    f"{len(converted.attachments)} attachments beyond depth {MAX_DEPTH}"
                )
            return rows
        seen: set[str] = set()
        for i, (raw, data) in enumerate(converted.attachments, start=1):
            att_name = attachment_name(raw, i)
            if att_name in seen:
                stem, dot, suffix = att_name.rpartition(".")
                att_name = f"{stem}-{i}.{suffix}" if dot and stem else f"{att_name}-{i}"
            seen.add(att_name)
            saved = self.out_dir / "attachments" / name / att_name
            saved.parent.mkdir(parents=True, exist_ok=True)
            saved.write_bytes(data)
            part = att_name if src.part is None else f"{src.part}/{att_name}"
            with self.name_lock:
                att_out = unique_name(
                    flat_name(Path(f"{name}__{att_name}")), self.taken, f"{src.path}#{part}"
                )
            rows += self.one(saved, Src(src.path, part=part), att_out, depth + 1)
        return rows


def run(args: argparse.Namespace, settings: dict[str, Any]) -> Envelope:
    env = Envelope(command=NAME, version=__version__)
    out_dir = Path(settings["out_dir"]) / "read"
    files = list(iter_files(args.paths))
    reader = Reader(out_dir, flat_names(files, common_root(args.paths)))
    env.inputs = [{"path": str(p)} for p in args.paths]

    rows: list[dict[str, Any]] = []
    jobs = args.jobs or int(settings.get("read", {}).get("jobs", 8))
    with ThreadPoolExecutor(max(1, jobs)) as pool:
        for result in pool.map(lambda f: reader.one(f, Src(str(f))), files):
            rows += result
    rows.sort(key=lambda r: r["cite"])

    index = out_dir / "index.jsonl"
    index.unlink(missing_ok=True)
    append_jsonl(index, rows)
    env.results = rows
    env.artifact(str(index), "results")

    errors = [r for r in rows if r.get("error")]
    pending = [r for r in rows if r["needs"]]
    env.summary = (
        f"{len(rows)} items melted into {out_dir}, "
        f"{len(errors)} errors, {len(pending)} need more work"
    )
    for r in errors:
        env.warnings.append(
            f"{r['cite']}: {r['error']}" + (f" ({r['hint']})" if r.get("hint") else "")
        )
    return env
