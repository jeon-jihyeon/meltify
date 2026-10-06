import contextvars
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from meltify.converters import quicklook
from meltify.converters.run import RunContext, current, use

KEYNOTE = (
    '<html><head><link rel="stylesheet" href="Attachment4.css"><title>k.key</title></head>'
    '<body><div><img src="Attachment3.pdf"></div><div><img src="Attachment1.pdf"></div>'
    "</body></html>"
)
IWORK = {"BaseBundlePath": "/System/Library/QuickLook/iWork.qlgenerator"}
OFFICE = {
    "BaseBundlePath": "/System/Library/QuickLook/Office.qlgenerator",
    "PageElementXPath": "/html/body/div",
    "Width": 753.0,
    "Height": 553.0,
}


def _run(enabled: bool = True) -> contextvars.Context:
    """A context set up the way read sets one up for each run"""
    run = contextvars.copy_context()
    run.run(use, RunContext(quicklook=enabled))
    return run


def _pdf(path: Path, text: str = "", width: float = 200, height: float = 100) -> Path:
    import pymupdf

    path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    page = doc.new_page(width=width, height=height)
    if text:
        page.insert_text((10, 30), text)
    doc.save(path)
    return path


def _preview(folder: Path, html: str | None, props: dict, files: dict[str, str]) -> Path:
    import plistlib

    folder.mkdir(parents=True)
    if html is not None:
        (folder / "Preview.html").write_text(html, encoding="utf-8")
    (folder / "PreviewProperties.plist").write_bytes(plistlib.dumps(props))
    for name, text in files.items():
        if name.endswith(".pdf"):
            _pdf(folder / name, text)
        else:
            (folder / name).write_text(text, encoding="utf-8")
    return folder


def _fake_qlmanage(monkeypatch, build, code=0):
    """qlmanage that writes the preview `build` makes into its -o dir"""
    calls = []

    def run(args, timeout):
        calls.append(list(args))
        if args[0] == "qlmanage" and code == 0:
            out = Path(args[args.index("-o") + 1])
            build(out / f"{Path(args[-1]).name}.qlpreview")
        return code

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(quicklook.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(quicklook, "previewable", lambda path: True)
    monkeypatch.setattr(quicklook, "launch", run)
    return calls


def _texts(pdf: Path) -> list[str]:
    import pymupdf

    with pymupdf.open(pdf) as doc:
        return [p.get_text().strip() for p in doc]


def test_off_macos_or_turned_off_nothing_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(quicklook, "launch", lambda *a: pytest.fail("qlmanage ran"))
    monkeypatch.setattr(sys, "platform", "linux")
    assert not quicklook.available()
    assert quicklook.to_pdf(tmp_path / "a.key") is None
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(quicklook.shutil, "which", lambda name: "/usr/bin/qlmanage")
    assert not _run(enabled=False).run(quicklook.available)


def test_a_hung_run_is_killed_with_its_group(monkeypatch):
    killed = []

    class Popen:
        def __init__(self, args, **kwargs):
            assert kwargs["start_new_session"] is True
            self.pid = 4242
            self.waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired("qlmanage", timeout)
            return -9

    monkeypatch.setattr(quicklook.subprocess, "Popen", Popen)
    monkeypatch.setattr(quicklook.os, "killpg", lambda pid, sig: killed.append(pid))
    assert quicklook.launch(["qlmanage", "-p", "a.key"], 1) is None
    assert killed == [4242]


def test_iwork_pages_merge_in_the_order_the_preview_shows_them(tmp_path, monkeypatch):
    def build(folder):
        files = {"Attachment1.pdf": "second slide", "Attachment3.pdf": "first slide"}
        _preview(folder, KEYNOTE, IWORK, {**files, "Attachment4.css": ""})

    calls = _fake_qlmanage(monkeypatch, build)
    pdf = quicklook.to_pdf(tmp_path / "k.key", out_dir=tmp_path / "out")
    assert pdf == tmp_path / "out" / "k.pdf"
    assert _texts(pdf) == ["first slide", "second slide"]
    assert calls[0][:3] == ["qlmanage", "-p", "-o"]


def test_numbers_sheets_in_unlinked_pages_still_count(tmp_path, monkeypatch):
    def build(folder):
        html = '<html><body><a>Sheet 1</a><iframe src="Attachment5.html"></iframe></body></html>'
        _preview(
            folder,
            html,
            IWORK,
            {
                "Attachment5.html": '<body><img src="Attachment2.pdf"></body>',
                "Attachment6.html": '<body><img src="Attachment7.pdf"></body>',
                "Attachment2.pdf": "sheet one",
                "Attachment7.pdf": "sheet two",
            },
        )

    _fake_qlmanage(monkeypatch, build)
    pdf = quicklook.to_pdf(tmp_path / "n.numbers", out_dir=tmp_path / "out")
    assert _texts(pdf) == ["sheet one", "sheet two"]


def test_old_pages_preview_pdf_is_used_as_is(tmp_path, monkeypatch):
    def build(folder):
        _preview(folder, None, IWORK, {"Preview.pdf": "pages 09"})

    _fake_qlmanage(monkeypatch, build)
    assert _texts(quicklook.to_pdf(tmp_path / "p.pages", out_dir=tmp_path / "o")) == ["pages 09"]


@pytest.mark.parametrize(
    ("html", "props", "want"),
    [
        # A lone picture on a slide is the picture's own vector PDF
        ('<div class="slide"><img src="Attachment1.pdf"></div>', OFFICE, ["Attachment1.pdf"]),
        ('<div class="slide"><p>Title</p><img src="Attachment1.pdf"></div>', OFFICE, None),
        ('<div><img src="Attachment1.pdf"><img src="Attachment2.pdf"></div>', OFFICE, None),
        ('<div><img src="Attachment1.png"></div>', IWORK, None),
        ('<div><img src="http://example.com/a.pdf"></div>', IWORK, None),
        ('<div><img src="../a.pdf"></div>', IWORK, None),
        ("<div><p>Keynote 09 text</p></div>", IWORK, None),
    ],
)
def test_which_previews_are_plain_page_pdfs(tmp_path, html, props, want):
    folder = _preview(tmp_path / "p", html, props, {"Attachment1.pdf": "", "Attachment2.pdf": ""})
    got = quicklook._attached_pdfs(folder, props)
    assert (None if got is None else [p.name for p in got]) == want


def _fake_webkit(monkeypatch, pages: list[str], mode: str, hang: bool = False):
    seen = {}

    class Popen:
        def __init__(self, args, stdout=None, **kwargs):
            seen["args"] = args
            seen["html"] = Path(args[4]).read_text()
            self.pid = 77
            out = Path(args[5])
            for i, text in enumerate(pages):
                _pdf(out / f"{i:04}.pdf", text, 400 * quicklook.ZOOM, 300 * quicklook.ZOOM)
            stdout.write(mode + "\n")

        def wait(self, timeout=None):
            if hang and timeout is not None:
                raise subprocess.TimeoutExpired("osascript", timeout)
            return 0

    monkeypatch.setattr(quicklook.subprocess, "Popen", Popen)
    monkeypatch.setattr(quicklook.os, "killpg", lambda pid, sig: seen.setdefault("killed", pid))
    return seen


def _office(folder):
    html = (
        "<html><head><style>div.slide{width:720}</style></head><body>"
        '<div class="slide"><p>Hello</p><img src="http://tracker.example/x.png"></div>'
        "</body></html>"
    )
    _preview(folder, html, OFFICE, {})


def test_office_html_is_printed_by_webkit_one_page_per_slide(tmp_path, monkeypatch):
    _fake_qlmanage(monkeypatch, _office)
    seen = _fake_webkit(monkeypatch, ["slide one", ""], "pages")
    pdf = quicklook.to_pdf(tmp_path / "a.ppt", out_dir=tmp_path / "out")
    # A blank slide keeps its place, so slide numbers still line up
    assert _texts(pdf) == ["slide one", ""]
    import pymupdf

    with pymupdf.open(pdf) as doc:
        assert tuple(doc[0].rect)[2:] == (400, 300)
    args = seen["args"]
    assert args[:3] == ["osascript", "-l", "JavaScript"]
    assert args[6:] == ["753.0", "553.0", "/html/body/div", str(quicklook.ZOOM)]
    # Remote pictures in the preview are blocked before WebKit loads it
    assert seen["html"].index(quicklook.CSP) < seen["html"].index("tracker.example")


def test_blank_bands_past_the_text_are_dropped(tmp_path, monkeypatch):
    _fake_qlmanage(monkeypatch, _office)
    _fake_webkit(monkeypatch, ["page text", "", ""], "bands")
    pdf = quicklook.to_pdf(tmp_path / "a.doc", out_dir=tmp_path / "out")
    assert _texts(pdf) == ["page text"]


def test_hung_webkit_turns_it_off_for_the_rest_of_the_run(tmp_path, monkeypatch):
    _fake_qlmanage(monkeypatch, _office)
    seen = _fake_webkit(monkeypatch, [], "pages", hang=True)
    run = _run()
    assert run.run(lambda: quicklook.to_pdf(tmp_path / "a.ppt", out_dir=tmp_path / "out")) is None
    assert seen["killed"] == 77 and run.run(current).webkit.working is False
    seen.clear()
    assert run.run(lambda: quicklook.to_pdf(tmp_path / "b.ppt", out_dir=tmp_path / "out")) is None
    assert "args" not in seen
    # The next run, or a window server that came back, gets WebKit again
    later = _run().run(lambda: quicklook.to_pdf(tmp_path / "c.ppt", out_dir=tmp_path / "out"))
    assert later is None
    assert "args" in seen


@pytest.mark.parametrize("code", [1, None])
def test_failed_or_hung_qlmanage_gives_none_and_cleans_up(tmp_path, monkeypatch, code):
    made = []
    real = quicklook.tempfile.mkdtemp

    def workdir(prefix):
        made.append(Path(real(prefix=prefix, dir=tmp_path)))
        return made[-1]

    monkeypatch.setattr(quicklook.run, "workdir", workdir)
    _fake_qlmanage(monkeypatch, _office, code=code)
    assert quicklook.to_pdf(tmp_path / "a.ppt") is None
    assert made and not any(Path(p).exists() for p in made)


def test_types_without_a_preview_never_start_qlmanage(tmp_path, monkeypatch):
    _fake_qlmanage(monkeypatch, _office)
    monkeypatch.setattr(quicklook, "previewable", lambda path: False)
    monkeypatch.setattr(quicklook, "launch", lambda *a: pytest.fail("qlmanage ran"))
    assert quicklook.to_pdf(tmp_path / "a.xyz") is None


@pytest.mark.macos
@pytest.mark.skipif(
    sys.platform != "darwin" or not (shutil.which("qlmanage") and shutil.which("osascript")),
    reason="needs Quick Look and osascript",
)
def test_real_quicklook_renders_every_slide_of_a_pptx(tmp_path):
    pptx = pytest.importorskip("pptx")

    deck = pptx.Presentation()
    for title in ("Quarterly revenue grew", "Churn fell in March"):
        slide = deck.slides.add_slide(deck.slide_layouts[1])
        slide.shapes.title.text = title
        slide.placeholders[1].text = f"Details for {title.lower()}"
    path = tmp_path / "deck.pptx"
    deck.save(path)
    run = _run()
    pdf = run.run(lambda: quicklook.to_pdf(path, out_dir=tmp_path / "out"))
    if pdf is None and not run.run(current).webkit.working:
        pytest.skip("WebKit timed out, likely no window server")
    assert pdf is not None
    texts = _texts(pdf)
    assert len(texts) == 2
    assert "Quarterly revenue grew" in texts[0] and "Churn fell in March" in texts[1]
