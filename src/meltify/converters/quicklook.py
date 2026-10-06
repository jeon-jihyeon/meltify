"""Quick Look renders on macOS, for files no installed program can convert

`qlmanage -p` writes the same preview Finder shows. iWork files come out as one PDF per
page, slide or sheet. Office files come out as HTML, which WebKit lays out and prints to
PDF one slide at a time
"""

from __future__ import annotations

import os
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from collections.abc import Sequence
from html.parser import HTMLParser
from pathlib import Path

from meltify import safe
from meltify.converters import run

TIMEOUT = 60
PREVIEW_TIMEOUT = 30
MDLS_TIMEOUT = 10
# Type answers kept per path, mtime and size, a few hundred bytes each at most
MAX_TYPES = 4096
_TYPES: dict[tuple[str, int, int], bool] = {}
_TYPES_LOCK = threading.Lock()
# WebKit draws at 2x, so vector shapes it rasterizes stay sharp enough for OCR
ZOOM = 2
# Viewport when the preview doesn't name one, about a letter page in CSS pixels
VIEWPORT = (816.0, 1056.0)
# The preview is built from the document, which may link remote pictures. Loading them
# would tell a server the file was opened
CSP = (
    '<meta http-equiv="Content-Security-Policy" '
    "content=\"default-src file: data: 'unsafe-inline' 'unsafe-eval'\">"
)

# Lays out a preview in WKWebView and writes one PDF per page element, or per page-tall
# band for documents without one. argv: html, out dir, width, height, xpath, zoom
SCRIPT = r"""
ObjC.import('Cocoa');
ObjC.import('WebKit');

function wait(until, ms) {
  var loop = $.NSRunLoop.currentRunLoop, t0 = Date.now();
  while (!until() && Date.now() - t0 < ms) {
    loop.runUntilDate($.NSDate.dateWithTimeIntervalSinceNow(0.05));
  }
  return until();
}

function layout(xp, band) {
  var out = [], doc = document.documentElement;
  if (xp) {
    var hits = document.evaluate(xp, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
    for (var i = 0; i < hits.snapshotLength; i++) {
      var b = hits.snapshotItem(i).getBoundingClientRect();
      if (b.width > 0 && b.height > 0) {
        out.push([b.left + scrollX, b.top + scrollY, b.width, b.height]);
      }
    }
  }
  if (out.length) return JSON.stringify({mode: 'pages', rects: out});
  // Cut bands at the top of a block, so no line of text is split across two pages
  var blocks = document.querySelectorAll('p,li,tr,img,h1,h2,h3,h4,h5,h6,table,pre');
  var tops = [].map.call(blocks, function (e) { return e.getBoundingClientRect().top + scrollY; });
  var w = doc.scrollWidth, h = doc.scrollHeight, y = 0;
  while (y < h - 1) {
    var end = Math.min(y + band, h);
    var cuts = tops.filter(function (t) { return t > y + band / 2 && t <= end; });
    if (end < h && cuts.length) end = Math.max.apply(null, cuts);
    out.push([0, y, w, end - y]);
    y = end;
  }
  return JSON.stringify({mode: 'bands', rects: out});
}

function run(argv) {
  var src = argv[0], outDir = argv[1], width = +argv[2], height = +argv[3];
  var xpath = argv[4], zoom = +argv[5];
  $.NSApplication.sharedApplication;
  var view = $.WKWebView.alloc.initWithFrameConfiguration(
    $.NSMakeRect(0, 0, width, height), $.WKWebViewConfiguration.alloc.init);
  view.pageZoom = zoom;
  var url = $.NSURL.fileURLWithPath(src);
  view.loadFileURLAllowingReadAccessToURL(url, url.URLByDeletingLastPathComponent);
  if (!wait(function () { return !view.isLoading; }, 30000)) throw new Error('page did not load');
  wait(function () { return false; }, 300);
  var probe = '(' + layout.toString() + ')(' + JSON.stringify(xpath) + ', ' + height + ')';
  var found = null, failed = false;
  view.evaluateJavaScriptCompletionHandler(probe, function (value, error) {
    if (value && !value.isNil()) found = JSON.parse(ObjC.unwrap(value)); else failed = true;
  });
  if (!wait(function () { return found || failed; }, 10000) || !found) throw new Error('no layout');
  for (var i = 0; i < found.rects.length; i++) {
    var r = found.rects[i], config = $.WKPDFConfiguration.alloc.init, done = false;
    var name = outDir + '/' + ('0000' + i).slice(-4) + '.pdf';
    config.rect = $.NSMakeRect(r[0] * zoom, r[1] * zoom, r[2] * zoom, r[3] * zoom);
    view.createPDFWithConfigurationCompletionHandler(config, function (data, error) {
      if (data && !data.isNil()) data.writeToFileAtomically(name, true);
      done = true;
    });
    if (!wait(function () { return done; }, 30000)) throw new Error('pdf timed out');
  }
  return found.mode;
}
"""


def enabled() -> bool:
    """Whether a full read would use Quick Look, which --shallow still keeps from starting"""
    if not run.current().quicklook or sys.platform != "darwin":
        return False
    return shutil.which("qlmanage") is not None


def available() -> bool:
    return enabled() and not run.current().shallow


def launch(args: Sequence[str], timeout: float) -> int | None:
    """Exit code, or None after killing a run that went past `timeout`

    Quick Look helpers can hang until killed, and they spawn their own helpers, so each
    run gets its own process group and the kill takes the whole group
    """
    proc = subprocess.Popen(
        list(args),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        return None


def previewable(path: Path) -> bool:
    """Whether Spotlight's type tree makes the file a document or picture

    qlmanage hangs until killed on types without a generator, so every Quick Look and
    Spotlight reader asks this first. The answer is kept per file version, since one
    fallback asks it up to three times
    """
    try:
        resolved = path.resolve()
        st = resolved.stat()
    except OSError:
        return False
    key = (str(resolved), st.st_mtime_ns, st.st_size)
    with _TYPES_LOCK:
        if key in _TYPES:
            return _TYPES[key]
    try:
        proc = safe.run(
            ["mdls", "-raw", "-name", "kMDItemContentTypeTree", str(resolved)],
            timeout=MDLS_TIMEOUT, check=False,
        )  # fmt: skip
        found = '"public.content"' in proc.stdout
    except (OSError, subprocess.TimeoutExpired):
        found = False
    with _TYPES_LOCK:
        if len(_TYPES) >= MAX_TYPES:
            _TYPES.clear()
        _TYPES[key] = found
    return found


class _Refs(HTMLParser):
    """Embedded files in document order, and whether any text shows outside them"""

    HIDDEN = ("script", "style", "title")

    def __init__(self) -> None:
        super().__init__()
        self.refs: list[str] = []
        self.text = False
        self._hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.HIDDEN:
            self._hidden += 1
        if tag in ("img", "iframe", "embed", "object"):
            found = dict(attrs)
            ref = found.get("src") or found.get("data")
            if ref:
                self.refs.append(ref)

    def handle_endtag(self, tag: str) -> None:
        if tag in self.HIDDEN and self._hidden:
            self._hidden -= 1

    def handle_data(self, data: str) -> None:
        if not self._hidden and data.strip():
            self.text = True


def _refs(page: Path) -> _Refs:
    found = _Refs()
    found.feed(page.read_text("utf-8", errors="replace"))
    return found


def _number(page: Path) -> int:
    digits = re.sub(r"\D", "", page.stem)
    return int(digits) if digits else 0


def _attached_pdfs(preview: Path, props: dict) -> list[Path] | None:
    """The page PDFs an iWork preview shows, in order, or None when it needs WebKit

    iWork previews hold nothing but one PDF per page, slide or sheet. Numbers puts each
    sheet in its own HTML file and links only the first, so the rest come in name order.
    An Office preview of a lone picture is one slide holding one PDF and no text, and
    that PDF is the picture itself, still vector
    """
    main = preview / "Preview.html"
    top = _refs(main)
    iwork = "iWork" in str(props.get("BaseBundlePath", ""))
    # A slide of several PDFs is shapes laid out together, not pages
    if not iwork and (top.text or len(top.refs) != 1):
        return None
    pages = [main, *sorted(set(preview.glob("*.html")) - {main}, key=_number)]
    found: list[Path] = []
    for page in pages:
        for ref in _refs(page).refs if page != main else top.refs:
            name = ref.split("?")[0].split("#")[0]
            if "/" in name or name.startswith("."):
                return None
            if name.lower().endswith((".html", ".htm")):
                continue
            if not name.lower().endswith(".pdf"):
                return None
            if (preview / name).is_file() and preview / name not in found:
                found.append(preview / name)
    return found or None


def _webkit(preview: Path, props: dict, out: Path, timeout: float) -> tuple[list[Path], bool]:
    """Page PDFs WebKit printed, and whether they're bands cut from one long page"""
    webkit = run.current().webkit
    if not webkit.working or shutil.which("osascript") is None:
        return [], False
    html = (preview / "Preview.html").read_text("utf-8", errors="replace")
    head = re.search(r"<head[^>]*>|<html[^>]*>", html, re.IGNORECASE)
    at = head.end() if head else 0
    page = preview / "meltify-preview.html"
    page.write_text(html[:at] + CSP + html[at:], encoding="utf-8")
    script = out.parent / "meltify-pages.js"
    script.write_text(SCRIPT, encoding="utf-8")
    out.mkdir(parents=True, exist_ok=True)
    width = float(props.get("Width") or VIEWPORT[0])
    height = float(props.get("Height") or VIEWPORT[1])
    xpath = str(props.get("PageElementXPath") or "")
    mode = out.parent / "meltify-mode.txt"
    # osascript prints the script's result, the only way to learn which layout it used
    with mode.open("w") as log:
        proc = subprocess.Popen(
            ["osascript", "-l", "JavaScript", str(script), str(page), str(out),
             str(width), str(height), xpath, str(ZOOM)],
            stdout=log, stderr=subprocess.DEVNULL, start_new_session=True,
        )  # fmt: skip
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            webkit.working = False
            return [], False
    if code != 0:
        return [], False
    return sorted(out.glob("*.pdf")), mode.read_text().strip() == "bands"


def _merge(parts: list[tuple[Path, float]], bands: bool, target: Path) -> Path | None:
    import pymupdf

    with run.LOCK, pymupdf.open() as out:
        for part, scale in parts:
            with pymupdf.open(part) as doc:
                for page in doc:
                    # A band past the end of the text is blank, unlike a slide, whose number
                    # citations count on
                    if bands and page.get_pixmap(dpi=10).is_unicolor:
                        continue
                    r = page.rect
                    new = out.new_page(width=r.width * scale, height=r.height * scale)
                    new.show_pdf_page(new.rect, doc, page.number)
        if not len(out):
            return None
        out.save(target, garbage=3, deflate=True)
    return target


def _rendered(path: Path, work: Path, target: Path, timeout: float) -> Path | None:
    preview_dir = work / "preview"
    preview_dir.mkdir()
    args = ["qlmanage", "-p", "-o", str(preview_dir), str(path.resolve())]
    if launch(args, PREVIEW_TIMEOUT) != 0:
        return None
    preview = next(preview_dir.glob("*.qlpreview"), None)
    if preview is None:
        return None
    if (preview / "Preview.pdf").is_file():
        return _merge([(preview / "Preview.pdf", 1.0)], False, target)
    if not (preview / "Preview.html").is_file():
        return None
    props: dict = {}
    if (preview / "PreviewProperties.plist").is_file():
        with (preview / "PreviewProperties.plist").open("rb") as f:
            props = plistlib.load(f)
    pdfs = _attached_pdfs(preview, props)
    if pdfs:
        return _merge([(p, 1.0) for p in pdfs], False, target)
    pages, bands = _webkit(preview, props, work / "pages", timeout)
    return _merge([(p, 1 / ZOOM) for p in pages], bands, target) if pages else None


def to_pdf(path: Path, *, out_dir: Path | None = None, timeout: float = TIMEOUT) -> Path | None:
    """PDF of a Quick Look preview, one page per slide where the preview has slides

    None off macOS, for types without a preview, and when Quick Look or WebKit fails or
    hangs. Without `out_dir` the PDF lands in a fresh temp dir that the caller removes
    """
    if not available() or not previewable(path):
        return None
    folder = run.workdir("meltify-quicklook-") if out_dir is None else out_dir
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{path.stem}.pdf"
    try:
        with tempfile.TemporaryDirectory(prefix="meltify-preview-") as work:
            pdf = _rendered(path, Path(work), target, timeout)
    except (OSError, RuntimeError, ValueError, plistlib.InvalidFileException):
        # PyMuPDF reports a broken PDF as a RuntimeError subclass
        pdf = None
    if pdf is None and out_dir is None:
        shutil.rmtree(folder, ignore_errors=True)
    return pdf
