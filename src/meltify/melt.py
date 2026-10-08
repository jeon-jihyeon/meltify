"""The read run's per-item stage: convert each input, write its markdown and queue its OCR"""

from __future__ import annotations

import contextvars
import hashlib
import importlib
import os
import shutil
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from meltify import passwords
from meltify.converters import Block, Converted, RecognizeJob, place
from meltify.converters.run import current
from meltify.evidence import Src, finding
from meltify.files import flat_name, fresh, member_name, unique_name, work_name
from meltify.needs import ITEM_NOTE, error_note
from meltify.safe import MissingTool

if TYPE_CHECKING:
    from meltify.pdfpool import PdfPool

# How many container levels to follow, archives and attachments counted together
MAX_DEPTH = 3
# Pending reasons in the order they show up in a row's needs. Missing engines keep the
# plain wording, like `ocr`, since installing one is the fix
LEFT = {"engine": "", "budget": " (budget)", "failed": " (failed)"}
# Engines read every picture and recording of the item, and none of them held any text
NO_TEXT = "no text found"
# What a per-file boundary lets through. Anything else a parser raises, like the
# BaseException pyo3 turns a Rust panic into, fails only that file
FATAL = (KeyboardInterrupt, SystemExit, GeneratorExit)
# Converters that open documents with PyMuPDF, which isn't thread-safe. Those that start
# soffice first, like legacy for .ppt, take the lock themselves only around PyMuPDF
PYMUPDF = {"pdf"}
# Embedded pictures bigger than this wait for OCR in a temp file instead of in memory
PICTURE_SPILL_BYTES = 256 << 10
# Rows that point at a place inside an item rather than at an item, each with how the
# summary counts them and the table columns it adds
DETAIL: dict[str, tuple[str, tuple[str, ...]]] = {
    "disputed": ("disputed values", ("text",)),
    "hidden": ("hidden spans", ("text", "reasons")),
    "contrast": ("contrast pages", ("path",)),
    "frame": ("frames", ("reasons", "path")),
}


def job_needs(jobs: list[RecognizeJob]) -> list[str]:
    images = sum(j.kind == "image" for j in jobs)
    pages = [j.src.page for j in jobs if j.kind == "page"]
    needs = []
    if pages:
        needs.append("ocr pages " + ",".join(map(str, pages)))
    if images:
        needs.append("ocr" if images == 1 else f"ocr {images} images")
    if any(j.kind in ("audio", "video") for j in jobs):
        needs.append("media")
    return needs


@dataclass
class Output:
    """A converted item waiting for the recognition stage before its markdown is written"""

    converted: Converted
    target: Path
    origin: str
    row: dict[str, Any]


class Reader:
    def __init__(
        self,
        out_dir: Path,
        names: dict[Path, str],
        *,
        shallow: bool = False,
        defer: bool = False,
        fetch_opts: dict[str, Any] | None = None,
        whole: bool = False,
        media_conf: dict[str, Any] | None = None,
        webp: tuple[int, int] | None = None,
    ) -> None:
        self.out_dir = out_dir
        self.names = names
        # Folded the way unique_name compares them
        self.taken = {n.casefold() for n in names.values()}
        self.shallow = shallow
        # Hold every item's markdown until recognition, so a run that fails there writes none
        self.defer = defer
        self.fetch_opts = fetch_opts or {}
        self.whole = whole
        self.media_conf = media_conf or {}
        # Longest side and quality of the WebP saved per picture, None for none
        self.webp = webp
        self.outputs: list[Output] = []
        # Pictures only one engine read, counted once recognition is done
        self.unchecked = 0
        # Items read --hidden couldn't check, since their format has no hidden-text scan
        self.hidden_unchecked = 0
        from meltify.converters.run import LOCK

        # PyMuPDF isn't thread-safe, and nested items can be PDFs too. Shared with the
        # renderer, which rasterizes pages for converters outside PYMUPDF
        self.pdf_lock = LOCK
        self.name_lock = threading.Lock()
        self.out_lock = threading.Lock()
        # Set by the run. Nested items go to `pool` as their own tasks, and PDFs to
        # `pdf_pool`, whose processes each hold their own PyMuPDF
        self.pool: ThreadPoolExecutor | None = None
        self.pdf_pool: PdfPool | None = None
        self.futures: list[Future[list[dict[str, Any]]]] = []
        self.spill: Path | None = None
        self.spilled = 0

    def _convert(
        self, convert: Callable[..., Converted], path: Path, src: Src, row: dict[str, Any]
    ) -> Converted:
        try:
            if row["kind"] in PYMUPDF:
                if self.pdf_pool is not None and (got := self.pdf_pool.convert(convert, path, src)):
                    return got
                with self.pdf_lock:
                    return convert(path, src)
            return convert(path, src)
        except (passwords.Locked, *FATAL):
            raise
        except BaseException as e:  # noqa: BLE001
            return self._rescue(path, src, e, row)

    def _rescue(self, path: Path, src: Src, error: BaseException, row: dict[str, Any]) -> Converted:
        """A render of a file its native converter gave up on, or the error again

        Encrypted and DRM-locked files are never rendered, since a render would show only
        the lock. A missing extra stays the answer unless a render read something
        """
        from meltify.converters import fallback, unlock

        if path.suffix.lower() not in fallback.NATIVE or unlock.sealed(path):
            raise error
        if isinstance(error, MissingTool):
            out = fallback.convert(path, src, error.name)
            if out.kind == "unknown":
                raise error
            out.needs.append(error.name)
            row["hint"] = error.hint
            return out
        why = f"{path.suffix.lower()[1:]} not read ({error_note(error)})"
        return fallback.convert(path, src, why)

    def _melt(self, path: Path, src: Src, row: dict[str, Any]) -> Converted:
        """Convert through the registry, a decrypted copy standing in for a locked file

        The copy keeps the file's name, so the same converter reads it, while cites and
        outputs stay on the original. It goes once nothing waits to OCR its pages
        """
        from meltify.converters import pick
        from meltify.converters.unlock import unlock

        row["kind"], convert = pick(path)
        opened = unlock(path)
        if opened == path:
            return self._convert(convert, path, src, row)
        converted = None
        try:
            row["kind"], convert = pick(opened)
            converted = self._convert(convert, opened, src, row)
            return converted
        finally:
            if converted is None or not any(j.path == opened for j in converted.jobs):
                shutil.rmtree(opened.parent, ignore_errors=True)

    def one(
        self, path: Path, src: Src, name: str | None = None, depth: int = 0
    ) -> list[dict[str, Any]]:
        row = finding(src, kind=None, chars=0, needs=[], hidden=0, out=None)
        try:
            converted = self._melt(path, src, row)
        except passwords.Locked as e:
            return [{**row, "needs": [str(e)]}]
        except MissingTool as e:
            return [{**row, "error": str(e), "hint": e.hint, "needs": [e.name]}]
        except FATAL:
            raise
        except BaseException as e:  # noqa: BLE001
            # One unreadable file shouldn't stop the rest of the folder
            return [{**row, "error": error_note(e, ITEM_NOTE)}]
        return self.keep(converted, name or self.names[path], depth, row, src.cite())

    def keep(
        self,
        converted: Converted,
        name: str,
        depth: int,
        row: dict[str, Any],
        origin: str,
    ) -> list[dict[str, Any]]:
        """Write the converted item's markdown, queue its OCR and melt its children"""
        if self.shallow:
            # Embedded pictures are only counted, so their bytes aren't held until the run
            # ends. They're listed the way a deep read without engines lists them
            pictures = [j for j in converted.jobs if j.src.img is not None]
            converted.jobs = [j for j in converted.jobs if j.src.img is None]
            converted.needs = job_needs(pictures) + converted.needs
        converted.jobs = [self._spilled(j) for j in converted.jobs]
        if current().pdf.hidden and not converted.hidden_checked:
            with self.out_lock:
                self.hidden_unchecked += 1
        target = self.out_dir / f"{name}.md"
        # Frames and renders from an earlier run would sit beside this run's in a grep. Pictures
        # go when the markdown linking them is rewritten, so a failed run keeps both
        for made in ("frames", "contrast"):
            shutil.rmtree(self.out_dir / "attachments" / name / made, ignore_errors=True)
        row.update(
            kind=converted.kind,
            needs=list(converted.needs),
            hidden=converted.hidden,
            out=str(target),
        )
        if converted.jobs or self.defer:
            with self.out_lock:
                self.outputs.append(Output(converted, target, origin, row))
        else:
            # Nothing waits on recognition, so write it now instead of holding its text until
            # the last file is read
            row["chars"] = converted.chars
            self._prune_pictures(name, set())
            target.parent.mkdir(parents=True, exist_ok=True)
            converted.write(target, origin)
        rows = [row, *self._details(converted, name, target)]
        children, converted.children = converted.children, []
        if not children:
            return rows
        if depth >= MAX_DEPTH:
            row["needs"].append(f"{len(children)} nested items beyond depth {MAX_DEPTH}")
            return rows
        members: set[str] = set()
        stored: set[str] = set()
        ready = []
        # Each member's bytes go once it's on disk, so a big archive isn't held in memory twice
        children.reverse()
        for i in range(1, len(children) + 1):
            child = children.pop()
            member = fresh(member_name(child.name, i), members, i)
            saved = (
                self.out_dir
                / "attachments"
                / name
                / fresh(flat_name(Path(member.replace("/", "__"))), stored, i, fold=True)
            )
            saved.parent.mkdir(parents=True, exist_ok=True)
            if child.path is not None:
                shutil.move(child.path, saved)
            else:
                saved.write_bytes(child.data or b"")
            child_src = child.parent.inside(member)
            with self.name_lock:
                child_out = unique_name(
                    flat_name(Path(f"{name}__{member.replace('/', '__')}")),
                    self.taken,
                    child_src.cite(),
                )
            ready.append((saved, child_src, child_out))
        # Every member is on disk before any is melted, so a video finds the subtitles
        # stored beside it whatever order the container lists them in
        for saved, child_src, child_out in ready:
            rows += self.submit(self.one, saved, child_src, child_out, depth + 1)
        return rows

    def pictures(self) -> int:
        """Images and PDF pages waiting for OCR"""
        return sum(j.kind in ("image", "page") for o in self.outputs for j in o.converted.jobs)

    def _details(self, converted: Converted, name: str, target: Path) -> list[dict[str, Any]]:
        """Rows for the hidden spans and contrast renders of an item, as the run asked for"""
        rows = [{**s, "chars": 0, "needs": [], "out": str(target)} for s in converted.spans]
        renders, converted.renders = converted.renders, []
        for at, file in renders:
            saved = self.out_dir / "attachments" / name / "contrast" / file.name
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(file, saved)
            shutil.rmtree(file.parent, ignore_errors=True)
            rows.append(
                finding(at, kind="contrast", path=str(saved), chars=0, needs=[], out=str(target))
            )
        return rows

    def submit(self, fn: Callable[..., list[dict[str, Any]]], *args: Any) -> list[dict[str, Any]]:
        """Rows of `fn` now, or none yet when the run's pool takes it as a task of its own

        Nested items get no worker that waits on them, since a pool whose workers all wait
        on their own children would stop. `drain` collects the rows instead. The task runs
        in a copy of this context, which holds the run's password and switches
        """
        if self.pool is None:
            return fn(*args)
        task = self.pool.submit(contextvars.copy_context().run, fn, *args)
        with self.out_lock:
            self.futures.append(task)
        return []

    def drain(self) -> list[dict[str, Any]]:
        """Rows of every submitted task, nested ones included, once all of them are done"""
        rows: list[dict[str, Any]] = []
        while True:
            with self.out_lock:
                batch, self.futures = self.futures, []
            if not batch:
                return rows
            # A task submits its children before it ends, so they're in the next batch
            for task in batch:
                rows += task.result()

    def _spilled(self, job: RecognizeJob) -> RecognizeJob:
        """The job with big picture bytes moved to a temp file the run removes

        Every picture would otherwise stay in memory until OCR reads it after the last
        file. The file holds the same bytes, so the job keeps its OCR cache key
        """
        if job.data is None or len(job.data) <= PICTURE_SPILL_BYTES:
            return job
        from meltify.converters import run

        with self.out_lock:
            if self.spill is None:
                self.spill = run.workdir("meltify-pictures-")
            self.spilled += 1
            target = self.spill / f"{self.spilled}.bin"
        target.write_bytes(job.data)
        return RecognizeJob(job.kind, job.src, path=target, rect=job.rect)

    def url(self, url: str) -> list[dict[str, Any]]:
        src = Src(url)
        row = finding(src, kind="url", chars=0, needs=[], hidden=0, out=None)
        try:
            fetch = importlib.import_module("meltify.fetch")
            web = importlib.import_module("meltify.converters.web")
        except ImportError:
            return [{**row, "needs": ["url support missing"]}]
        with self.name_lock:
            name = unique_name(work_name(url), self.taken, url)
        try:
            got = fetch.fetch(url, self.out_dir / "web", fetch.FetchOptions(**self.fetch_opts))
            meta = {
                "fetched_at": got.fetched_at,
                "final_url": got.final_url,
                "sha256": got.sha256,
                "etag": got.etag,
            }
            row.update(meta)
            if got.kind == "media":
                converted = self._media(url, src, self.out_dir / "web" / name)
            elif got.kind == "html":
                src = Src(got.final_url)
                converted = web.convert_page(got.path, src, whole=self.whole)
                row.update(finding(src))
            else:
                converted = self._melt(got.path, src, row)
        except passwords.Locked as e:
            return [{**row, "needs": [str(e)]}]
        except MissingTool as e:
            return [{**row, "error": str(e), "hint": e.hint, "needs": [e.name]}]
        except FATAL:
            raise
        except BaseException as e:  # noqa: BLE001
            return [{**row, "error": error_note(e, ITEM_NOTE)}]
        # The URL as given goes last, so a rerun that fails to fetch it can still tell which
        # markdown the redirect target came from
        origin = " ".join([src.cite(), *(f"{k}={v}" for k, v in {**meta, "url": url}.items() if v)])
        return self.keep(converted, name, 0, row, origin)

    def _media(self, url: str, src: Src, work: Path) -> Converted:
        from meltify import video

        if self.shallow:
            return Converted("media", needs=["media"])
        run = current()
        clip, subs = video.download(
            url,
            work,
            list(self.media_conf["sub_langs"]),
            run.subs_only,
            int(self.media_conf["max_height"]),
            bool(self.fetch_opts.get("allow_private")),
        )
        return video.recording("video", src, clip, subs)

    def finish(self, recognize: Callable[..., list[Any]] | None) -> list[dict[str, Any]]:
        """Fold recognized text into each item, write its markdown and return its detail rows"""
        # Workers finish in any order, while OCR's budget goes to jobs in the order given
        self.outputs.sort(key=lambda o: o.row["cite"])
        jobs = [j for o in self.outputs for j in o.converted.jobs]
        outcomes = recognize(jobs) if recognize and jobs else None
        pictures = self._save_pictures(
            [(o.target.stem, j) for o in self.outputs for j in o.converted.jobs]
        )
        details: list[dict[str, Any]] = []
        n = 0
        for o in self.outputs:
            mine = o.converted.jobs
            got = outcomes[n : n + len(mine)] if outcomes is not None else None
            links = pictures[n : n + len(mine)]
            n += len(mine)
            if got is None:
                left = job_needs(mine)
            else:
                left = [
                    need + suffix
                    for reason, suffix in LEFT.items()
                    for need in job_needs(
                        [j for j, g in zip(mine, got, strict=True) if g.left == reason]
                    )
                ]
                o.converted.blocks = place(o.converted.blocks, [b for g in got for b in g.blocks])
                for g in got:
                    details += [
                        {**d, "chars": 0, "needs": [], "out": str(o.target)} for d in g.disputed
                    ]
                    details += self._frames(g.frames, o.target)
                    self.unchecked += g.unchecked
                # Tells a blank scan apart from one nothing could read. Single pictures inside a
                # document stay quiet, since most of them are photos and logos
                if mine and not left and not o.converted.chars:
                    left = [NO_TEXT]
            o.converted.needs = left + o.converted.needs
            o.row["needs"] = left + o.row["needs"]
            o.row["chars"] = o.converted.chars
            # After chars, since a link points at a picture and isn't text read from the item
            _link_pictures(o.converted, mine, links)
            self._prune_pictures(o.target.stem, {Path(link).name for link in links if link})
            o.target.parent.mkdir(parents=True, exist_ok=True)
            o.converted.write(o.target, o.origin)
        return details

    def _save_pictures(self, jobs: list[tuple[str, RecognizeJob]]) -> list[str | None]:
        """A small WebP per image job, as its path from the markdown, or None for no picture"""
        from meltify import imaging

        if not self.webp:
            return [None] * len(jobs)
        side, quality = self.webp
        digests: dict[Path, str] = {}

        def link(stem: str, job: RecognizeJob) -> str | None:
            if job.kind != "image":
                return None
            if job.path is None:
                digest = hashlib.sha256(job.data or b"").hexdigest()
            elif job.path not in digests:
                # Every kept frame of a TIFF is a job on the same file, so it's hashed once
                with job.path.open("rb") as f:
                    digest = digests[job.path] = hashlib.file_digest(f, "sha256").hexdigest()
            else:
                digest = digests[job.path]
            # The size and quality are in the name, so a rerun reuses only an identical file
            key = f"{digest}:{job.src.frame or 1}:{side}:{quality}"
            name = hashlib.sha256(key.encode()).hexdigest()[:16]
            return f"attachments/{stem}/pictures/{name}.webp"

        links = [link(stem, job) for stem, job in jobs]
        todo = {
            path: job
            for path, (_, job) in zip(links, jobs, strict=True)
            if path and not (self.out_dir / path).is_file()
        }

        def save(path: str) -> bool:
            job = todo[path]
            source = job.data if job.path is None else job.path
            assert source is not None
            return imaging.shrink(source, self.out_dir / path, side, quality, job.src.frame or 1)

        # Pillow decodes, resizes and encodes outside the GIL, so threads share the work
        with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 1)) as pool:
            failed = {path for path, ok in zip(todo, pool.map(save, todo), strict=True) if not ok}
        return [None if path in failed else path for path in links]

    def _prune_pictures(self, stem: str, keep: set[str]) -> None:
        """Remove pictures of an earlier run the markdown about to be written won't link"""
        folder = self.out_dir / "attachments" / stem / "pictures"
        if not keep:
            shutil.rmtree(folder, ignore_errors=True)
            return
        for file in folder.iterdir():
            if file.name not in keep:
                file.unlink(missing_ok=True)

    def _frames(self, frames: list[dict[str, Any]], target: Path) -> list[dict[str, Any]]:
        """Frame rows with each picture saved beside the item's markdown"""
        from meltify import imaging

        rows = []
        for f in frames:
            file = Path(f.pop("file"))
            folder = self.out_dir / "attachments" / target.stem / "frames"
            saved = folder / f"{file.stem}.webp"
            if not self.webp or not imaging.shrink(file, saved, *self.webp):
                saved = folder / file.name
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(file, saved)
            rows.append({**f, "path": str(saved), "chars": 0, "needs": [], "out": str(target)})
        return rows


def _link_pictures(converted: Converted, jobs: list[RecognizeJob], links: list[str | None]) -> None:
    """Link each picture first in its OCR block, or in a block of its own when it has none"""
    heads: dict[Src, Block] = {}
    for b in converted.blocks:
        heads.setdefault(b.src, b)
    rest = []
    for job, link in zip(jobs, links, strict=True):
        if link is None:
            continue
        line = f"![picture]({link})"
        block = heads.get(job.src)
        if block is not None:
            block.text = f"{line}\n{block.text}"
        else:
            heads[job.src] = Block(job.src, line)
            rest.append(heads[job.src])
    converted.blocks = place(converted.blocks, rest)
