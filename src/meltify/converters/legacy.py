"""Binary Office 97-2003 files: doc text by line, xls cells, ppt through a PDF render"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from meltify.converters import Block, Converted
from meltify.converters.sheet import _cell
from meltify.converters.text import decode, numbered
from meltify.evidence import Src
from meltify.safe import MissingTool, run

OFFICE_HINT = "meltify doctor --install office"
# Where the macOS app keeps the binary when it isn't linked onto PATH
MAC_SOFFICE = Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")
SOFFICE_TIMEOUT = 180


def soffice() -> str | None:
    found = shutil.which("soffice") or shutil.which("libreoffice")
    if found:
        return found
    return str(MAC_SOFFICE) if MAC_SOFFICE.exists() else None


def render(binary: str, path: Path, target: str, out_dir: Path) -> Path:
    # A private profile per call, since parallel soffice runs fight over the shared one
    profile = (out_dir / "profile").as_uri()
    run(
        [
            binary,
            "--headless",
            f"-env:UserInstallation={profile}",
            "--convert-to",
            target,
            "--outdir",
            str(out_dir),
            str(path),
        ],
        timeout=SOFFICE_TIMEOUT,
    )
    produced = out_dir / f"{path.stem}.{target.split(':')[0]}"
    if not produced.exists():
        raise RuntimeError(f"soffice wrote no {produced.suffix} for {path.name}")
    return produced


def _doc(path: Path, src: Src) -> Converted:
    out = Converted("legacy")
    failed = None
    try:
        from legacy_doc import extract_text
    except ImportError:
        extract_text = None
    if extract_text is not None:
        try:
            out.blocks = numbered(extract_text(path.read_bytes()).text, src)
            return out
        except Exception as e:  # noqa: BLE001
            # Word 95, odd encodings and oversized files land here, and soffice may still cope
            failed = f"{type(e).__name__}: {e}"[:200]
    binary = soffice()
    if binary is None:
        if extract_text is None:
            raise MissingTool("legacy-doc", OFFICE_HINT)
        out.needs.append(f"doc ({failed}, install LibreOffice to retry)")
        return out
    with tempfile.TemporaryDirectory(prefix="meltify-doc-") as tmp:
        txt = render(binary, path, "txt:Text (encoded):UTF8", Path(tmp))
        out.blocks = numbered(decode(txt.read_bytes()), src)
    return out


def _value(v: Any) -> Any:
    # calamine reads every number as a float, while sheet.py shows openpyxl ints as ints
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return None if v == "" else v


def _xls(path: Path, src: Src) -> Converted:
    try:
        from python_calamine import CalamineWorkbook, SheetTypeEnum, SheetVisibleEnum
    except ImportError as e:
        raise MissingTool("python-calamine", OFFICE_HINT) from e
    from openpyxl.utils import get_column_letter

    out = Converted("legacy")
    wb = CalamineWorkbook.from_path(str(path))
    hidden = []
    try:
        for meta in wb.sheets_metadata:
            if meta.typ != SheetTypeEnum.WorkSheet:
                continue
            if meta.visible != SheetVisibleEnum.Visible:
                hidden.append(meta.name)
            # Keep the empty top-left area, so row and column numbers match the sheet
            grid = wb.get_sheet_by_name(meta.name).to_python(skip_empty_area=False)
            rows = [
                (n, [_value(v) for v in r])
                for n, r in enumerate(grid, start=1)
                if any(v != "" for v in r)
            ]
            if not rows:
                continue
            width = max(len(r) for _, r in rows)
            letters = [get_column_letter(i + 1) for i in range(width)]
            lines = ["| row | " + " | ".join(letters) + " |", "|---" * (width + 1) + "|"]
            for n, r in rows:
                values = [_cell(v) for v in r] + [""] * (width - len(r))
                lines.append(f"| {n} | " + " | ".join(values) + " |")
            out.blocks.append(Block(replace(src, sheet=meta.name), "\n".join(lines)))
    finally:
        wb.close()
    if hidden:
        out.needs.append("hidden sheets " + ", ".join(hidden))
    return out


def _ppt(path: Path, src: Src) -> Converted:
    from meltify.converters import pdf

    binary = soffice()
    if binary is None:
        return Converted("legacy", needs=["ppt (install LibreOffice)"])
    tmp = Path(tempfile.mkdtemp(prefix="meltify-ppt-"))
    rendered = render(binary, path, "pdf", tmp)
    # Slides become PDF pages, and pdf cites through the src it's given, so cites stay `a.ppt#p2`
    out = pdf.convert(rendered, src)
    out.kind = "legacy"
    # The OCR stage opens the rendered PDF for image-only slides, so then it has to stay
    if not any(job.path is not None for job in out.jobs):
        shutil.rmtree(tmp, ignore_errors=True)
    return out


def convert(path: Path, src: Src) -> Converted:
    suffix = path.suffix.lower()
    if suffix == ".xls":
        return _xls(path, src)
    if suffix == ".ppt":
        return _ppt(path, src)
    return _doc(path, src)
