import contextlib
import contextvars
import io
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from meltify.converters import fallback, pick, quicklook, render
from meltify.converters import run as run_state
from meltify.converters.run import RunContext
from meltify.evidence import Src
from meltify.needs import LIBREOFFICE

BINARY = b"\x00\x01\x02binary"


def _png(width: int = 200, height: int = 120) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buf, "PNG")
    return buf.getvalue()


def _two_page_pdf(path: Path) -> Path:
    import pymupdf

    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Rendered page one holds the total 1,234")
    doc.new_page()
    doc.save(path)
    return path


def _fake_soffice(monkeypatch):
    def to_pdf(path, timeout=render.TIMEOUT, out_dir=None):
        return _two_page_pdf(out_dir / f"{path.stem}.pdf")

    monkeypatch.setattr(render, "to_pdf", to_pdf)


def _no_subprocess(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError(f"no subprocess expected, got {args[0]}")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)


def _no_media_or_preview(monkeypatch):
    monkeypatch.setattr(fallback, "_media", lambda path: None)
    monkeypatch.setattr(quicklook, "available", lambda: False)


def test_binary_unknown_reaches_the_fallback(tmp_path, monkeypatch):
    f = tmp_path / "a.wps"
    f.write_bytes(BINARY)
    _fake_soffice(monkeypatch)
    kind, convert = pick(f)
    out = convert(f, Src(str(f)))
    assert kind == "unknown" and out.kind == "rendered"


def test_wpg_graphics_render_instead_of_parsing_as_wordperfect(tmp_path, monkeypatch):
    # Same \xffWPC header as a WordPerfect document, file type 0x16 for WPG
    f = tmp_path / "chart.wpg"
    f.write_bytes(b"\xffWPC\x10\x00\x00\x00\x01\x16\x01\x00" + BINARY)
    _fake_soffice(monkeypatch)
    kind, convert = pick(f)
    assert kind == "unknown" and ".wpg" in fallback.SOFFICE
    assert convert(f, Src(str(f))).kind == "rendered"


def test_rendered_pdf_cites_pages_of_the_original(tmp_path, monkeypatch):
    f = tmp_path / "a.wps"
    f.write_bytes(BINARY)
    _fake_soffice(monkeypatch)
    out = fallback.convert(f, Src(str(f)))
    assert out.kind == "rendered"
    assert [b.src.cite() for b in out.blocks] == [f"{f}#p1"]
    assert "total 1,234" in out.blocks[0].text
    # The blank page goes to OCR, so the rendered PDF it points at must outlive the call
    [job] = out.jobs
    assert job.kind == "page" and job.src.cite() == f"{f}#p2" and job.path.is_file()
    shutil.rmtree(job.path.parent)


def test_rendered_pdf_without_ocr_pages_is_cleaned_up(tmp_path, monkeypatch):
    f = tmp_path / "a.wps"
    f.write_bytes(BINARY)
    made = []

    def to_pdf(path, timeout=render.TIMEOUT, out_dir=None):
        import pymupdf

        doc = pymupdf.open()
        doc.new_page().insert_text((72, 72), "Only text here, nothing to OCR at all")
        doc.save(out_dir / "a.pdf")
        made.append(out_dir)
        return out_dir / "a.pdf"

    monkeypatch.setattr(render, "to_pdf", to_pdf)
    out = fallback.convert(f, Src(str(f)))
    assert out.blocks and not out.jobs
    assert not made[0].exists()


def test_libreoffice_format_without_soffice_names_libreoffice(tmp_path, monkeypatch):
    f = tmp_path / "a.wps"
    f.write_bytes(BINARY)
    monkeypatch.setattr(render, "to_pdf", lambda path, timeout=0, out_dir=None: None)
    monkeypatch.setattr(render, "soffice", lambda: None)
    _no_media_or_preview(monkeypatch)
    out = fallback.convert(f, Src(str(f)))
    assert out.kind == "unknown"
    assert out.needs == [f"unsupported format ({LIBREOFFICE})"]
    assert "meltify doctor --install libreoffice" in LIBREOFFICE


def test_other_binaries_never_go_to_soffice(tmp_path, monkeypatch):
    f = tmp_path / "a.xyz"
    f.write_bytes(BINARY)

    def refuse(path, timeout=0, out_dir=None):
        raise AssertionError("soffice would render the bytes as text")

    monkeypatch.setattr(render, "to_pdf", refuse)
    _no_media_or_preview(monkeypatch)
    assert fallback.convert(f, Src(str(f))).needs == ["unsupported format"]


def test_missing_converter_need_is_kept(tmp_path, monkeypatch):
    f = tmp_path / "a.xyz"
    f.write_bytes(BINARY)
    _no_media_or_preview(monkeypatch)
    need = "xyz not read (ValueError: bad header)"
    assert fallback.convert(f, Src(str(f)), need).needs == [need]


@pytest.mark.parametrize(
    "context",
    [RunContext(fallback=False), RunContext(shallow=True)],
    ids=["disabled", "shallow"],
)
def test_disabled_or_shallow_starts_no_subprocess(tmp_path, monkeypatch, context):
    f = tmp_path / "a.wps"
    f.write_bytes(BINARY)
    monkeypatch.setattr(run_state, "current", lambda: context)
    _no_subprocess(monkeypatch)
    out = fallback.convert(f, Src(str(f)))
    assert out.kind == "unknown" and out.needs == ["unsupported format"] and not out.jobs


def test_a_run_context_holds_for_its_run_only():
    run = contextvars.copy_context()
    context = RunContext(fallback=False, shallow=True)
    run.run(run_state.use, context)
    assert run.run(run_state.current) is context
    assert run_state.current() == RunContext()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
@pytest.mark.parametrize(
    ("fmt", "source", "kind"),
    [("mp3", "sine=d=1", "audio"), ("matroska", "testsrc=d=1:s=64x64", "video")],
)
def test_media_under_an_unknown_name_is_transcribed(tmp_path, monkeypatch, fmt, source, kind):
    f = tmp_path / "clip.qqq"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", source, "-f", fmt, str(f)],
        check=True,
    )
    monkeypatch.setattr(quicklook, "to_pdf", lambda *a, **k: pytest.fail("not a document"))
    out = fallback.convert(f, Src(str(f)))
    assert out.kind == "media"
    assert [(j.kind, j.path) for j in out.jobs] == [(kind, f)]


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="needs ffprobe")
def test_garbage_and_single_images_are_not_media(tmp_path):
    junk = tmp_path / "a.xyz"
    junk.write_bytes(BINARY * 100)
    still = tmp_path / "b.xyz"
    still.write_bytes(_png())
    assert fallback._media(junk) is None
    assert fallback._media(still) is None


def _fake_thumbnail(monkeypatch, png: bytes | None, hang: bool = False):
    """Quick Look whose full preview fails, so only `qlmanage -t` is left"""
    calls = {"killed": []}
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(fallback, "_media", lambda path: None)
    monkeypatch.setattr(fallback, "_spotlight", lambda path, src: None)
    monkeypatch.setattr(quicklook, "previewable", lambda path: True)
    monkeypatch.setattr(quicklook, "to_pdf", lambda path, out_dir=None, timeout=0: None)

    class Popen:
        def __init__(self, args, **kwargs):
            calls["args"], calls["kwargs"] = args, kwargs
            self.pid = 4242
            self.waits = 0
            if png is not None:
                name = Path(args[-1]).name
                (Path(args[args.index("-o") + 1]) / f"{name}.png").write_bytes(png)

        def wait(self, timeout=None):
            self.waits += 1
            if hang and self.waits == 1:
                raise subprocess.TimeoutExpired(calls["args"], timeout)
            return 0

    monkeypatch.setattr(subprocess, "Popen", Popen)
    monkeypatch.setattr(quicklook.os, "killpg", lambda pid, sig: calls["killed"].append(pid))
    return calls


def test_quicklook_thumbnail_is_the_last_resort_image_job(tmp_path, monkeypatch):
    f = tmp_path / "a.xyz"
    f.write_bytes(BINARY)
    calls = _fake_thumbnail(monkeypatch, _png())
    out = fallback.convert(f, Src(str(f)))
    assert out.kind == "rendered"
    assert out.needs == ["first page only, read from a Quick Look preview"]
    [job] = out.jobs
    # Boxes from the preview cite the original file in pixels
    assert job.kind == "image" and job.src == Src(str(f)) and job.data == _png()
    assert calls["args"][:4] == ["qlmanage", "-t", "-s", str(fallback.PREVIEW_SIDE)]
    assert calls["args"][-1] == str(f.resolve())
    assert calls["kwargs"]["start_new_session"] is True


def test_hung_quicklook_is_killed_and_reported(tmp_path, monkeypatch):
    f = tmp_path / "a.xyz"
    f.write_bytes(BINARY)
    calls = _fake_thumbnail(monkeypatch, None, hang=True)
    out = fallback.convert(f, Src(str(f)))
    assert calls["killed"] == [4242]
    assert out.needs == ["unsupported format"] and not out.jobs


def test_tiny_preview_is_not_worth_reading(tmp_path, monkeypatch):
    f = tmp_path / "a.xyz"
    f.write_bytes(BINARY)
    _fake_thumbnail(monkeypatch, _png(16, 16))
    assert fallback.convert(f, Src(str(f))).needs == ["unsupported format"]


def _fake_full_preview(monkeypatch, seen):
    def to_pdf(path, out_dir=None, timeout=0):
        seen.append(path)
        return _two_page_pdf(out_dir / f"{path.stem}.pdf")

    monkeypatch.setattr(fallback, "_media", lambda path: None)
    monkeypatch.setattr(quicklook, "available", lambda: True)
    monkeypatch.setattr(quicklook, "to_pdf", to_pdf)
    monkeypatch.setattr(fallback, "_thumbnail", lambda path, src: pytest.fail("full preview won"))


def test_full_quicklook_preview_reads_every_page(tmp_path, monkeypatch):
    f = tmp_path / "a.xyz"
    f.write_bytes(BINARY)
    seen = []
    _fake_full_preview(monkeypatch, seen)
    out = fallback.convert(f, Src(str(f)))
    assert seen == [f] and out.kind == "rendered" and out.needs == []
    assert [b.src.cite() for b in out.blocks] == [f"{f}#p1"]
    [job] = out.jobs
    assert job.src.cite() == f"{f}#p2"
    shutil.rmtree(job.path.parent)


def test_libreoffice_formats_reach_quicklook_through_render(tmp_path, monkeypatch):
    f = tmp_path / "a.key"
    f.write_bytes(BINARY)
    seen = []
    _fake_full_preview(monkeypatch, seen)
    monkeypatch.setattr(render, "soffice", lambda: None)
    out = fallback.convert(f, Src(str(f)))
    # render tried it once, so the fallback doesn't ask Quick Look again
    assert seen == [f] and out.kind == "rendered"
    shutil.rmtree(out.jobs[0].path.parent)


def test_spotlight_text_comes_before_the_thumbnail(tmp_path, monkeypatch):
    f = tmp_path / "a.xyz"
    f.write_bytes(BINARY)
    monkeypatch.setattr(fallback, "_media", lambda path: None)
    monkeypatch.setattr(quicklook, "available", lambda: True)
    monkeypatch.setattr(quicklook, "to_pdf", lambda path, out_dir=None, timeout=0: None)
    monkeypatch.setattr(fallback, "_spotlight_text", lambda path: "Indexed text 42")
    monkeypatch.setattr(fallback, "_thumbnail", lambda path, src: pytest.fail("text won"))
    out = fallback.convert(f, Src(str(f)))
    assert out.kind == "text" and [b.text for b in out.blocks] == ["Indexed text 42"]
    assert out.blocks[0].src == Src(str(f))
    assert out.needs == ["text only, read by macOS Spotlight without layout or pictures"]


def test_spotlight_is_off_with_quicklook(tmp_path, monkeypatch):
    monkeypatch.setattr(quicklook, "available", lambda: False)
    monkeypatch.setattr(fallback, "_spotlight_text", lambda path: pytest.fail("ran"))
    assert fallback._spotlight(tmp_path / "a.xyz", Src("a.xyz")) is None


def test_mdimport_dump_is_unescaped(tmp_path, monkeypatch):
    dump = (
        "29 attributes returned\n{\n"
        '    kMDItemContentType = "com.example";\n'
        '    kMDItemTextContent = "He said \\"hi\\" \\\\ back\\ttab \\Ud83d\\Ude00 '
        '\\Ud55c\\Uae00\\nnext";\n}\n'
    )

    def run(args, **kwargs):
        assert args[:3] == ["mdimport", "-t", "-d3"]
        return subprocess.CompletedProcess(args, 0, "", dump)

    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(quicklook, "previewable", lambda path: True)
    monkeypatch.setattr(fallback.safe, "run", run)
    text = fallback._spotlight_text(tmp_path / "a.key")
    assert text == 'He said "hi" \\ back\ttab \U0001f600 \ud55c\uae00\nnext'


def test_importer_garbage_is_not_text(tmp_path, monkeypatch):
    dump = '    kMDItemTextContent = "\\U2013\\U0153\x11\\U2021\x1a\x03\x06\x01\x01\x01 x";'
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(quicklook, "previewable", lambda path: True)
    monkeypatch.setattr(
        fallback.safe, "run", lambda args, **kw: subprocess.CompletedProcess(args, 0, dump, "")
    )
    assert fallback._spotlight_text(tmp_path / "a.xyz") is None
    assert fallback._readable("plain words\n\tand a tab\f")
    assert not fallback._readable("   ")


def test_textutil_reads_word_formats_first(tmp_path, monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append(args[0])
        return subprocess.CompletedProcess(args, 0, "From textutil\n", "")

    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(quicklook, "previewable", lambda path: True)
    monkeypatch.setattr(fallback.safe, "run", run)
    assert fallback._spotlight_text(tmp_path / "a.doc") == "From textutil\n"
    assert calls == ["textutil"]
    # textutil reads anything else as plain text, so it never sees other types
    calls.clear()
    fallback._spotlight_text(tmp_path / "a.wps")
    assert calls == ["mdimport"]


@pytest.mark.macos
@pytest.mark.skipif(sys.platform != "darwin", reason="needs Quick Look")
def test_real_quicklook_previews_a_document_and_skips_unknown_types(tmp_path):
    doc = tmp_path / "note.txt"
    doc.write_text("Quick Look renders this line\n" * 40)
    out = fallback._thumbnail(doc, Src(str(doc)))
    assert out is not None and out.jobs[0].data.startswith(b"\x89PNG")
    # Without a generator qlmanage would hang, so an unknown type never gets that far
    junk = tmp_path / "a.xyz"
    junk.write_bytes(BINARY)
    assert quicklook.previewable(junk) is False


@pytest.mark.macos
@pytest.mark.skipif(sys.platform != "darwin", reason="needs Spotlight")
def test_real_spotlight_reads_rtf_text(tmp_path):
    rtf = tmp_path / "memo.rtf"
    rtf.write_text(r"{\rtf1\ansi Spotlight reads this memo\par}")
    assert "Spotlight reads this memo" in (fallback._spotlight_text(rtf) or "")


def _text_image(path: Path, fmt: str) -> Path:
    from PIL import Image, ImageDraw

    im = Image.new("RGB", (400, 100), "white")
    ImageDraw.Draw(im).text((10, 40), "PCX KIWI", fill="black")
    im.save(path, fmt)
    return path


def test_pillow_formats_without_a_suffix_entry_go_to_ocr(tmp_path, monkeypatch):
    f = _text_image(tmp_path / "scan.pcx", "PCX")
    monkeypatch.setattr(fallback, "_media", lambda path: pytest.fail("Pillow reads it"))
    kind, convert = pick(f)
    out = convert(f, Src(str(f)))
    assert kind == "unknown" and out.kind == "image"
    assert [(j.kind, j.path, j.src) for j in out.jobs] == [("image", f, Src(str(f)))]
    assert out.needs == []


def test_undecodable_image_header_is_not_a_picture(tmp_path):
    f = tmp_path / "a.pcx"
    f.write_bytes(_png()[:40])
    assert fallback._picture(f) is False


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="needs ffprobe")
def test_still_images_ffprobe_opens_are_not_media(tmp_path):
    # ffprobe gives a single SVG a 0.04s video stream, and ffmpeg then has no decoder for it
    svg = tmp_path / "pic.svgq"
    svg.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="10"/>')
    pcx = _text_image(tmp_path / "scan.pcxq", "PCX")
    assert fallback._media(svg) is None
    assert fallback._media(pcx) is None


def test_workdir_is_removed_at_exit_even_from_a_thread():
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(1) as pool:
        folder = pool.submit(run_state.workdir, "meltify-test-").result()
    assert folder.is_dir()
    run_state._cleanup()
    assert not folder.exists()


def test_rendered_pdf_with_ocr_pages_stays_until_its_run_ends(tmp_path, monkeypatch):
    f = tmp_path / "a.wps"
    f.write_bytes(BINARY)
    _fake_soffice(monkeypatch)
    temps = run_state.Temps()
    run = contextvars.copy_context()
    run.run(run_state.use, RunContext(temps=temps))
    out = run.run(fallback.convert, f, Src(str(f)))
    folder = out.jobs[0].path.parent
    # The OCR stage still has to open the PDF
    assert temps.folders == [folder] and out.jobs[0].path.is_file()
    temps.remove()
    assert not folder.exists() and folder not in run_state._LIVE


@pytest.mark.parametrize(
    "error", [RuntimeError("soffice exited 1"), subprocess.TimeoutExpired("soffice", 120)]
)
def test_a_failed_render_still_tries_the_other_readers(tmp_path, monkeypatch, error):
    def to_pdf(path, timeout=render.TIMEOUT, out_dir=None):
        raise error

    monkeypatch.setattr(render, "to_pdf", to_pdf)
    _no_media_or_preview(monkeypatch)
    picture = tmp_path / "scan.wps"
    picture.write_bytes(_png())
    out = fallback.convert(picture, Src(str(picture)))
    assert out.kind == "image" and len(out.jobs) == 1
    assert out.needs[-1].startswith("LibreOffice could not render it")

    seen = []
    monkeypatch.setattr(fallback, "_thumbnail", lambda path, src: seen.append(path))
    f = tmp_path / "a.wps"
    f.write_bytes(BINARY)
    out = fallback.convert(f, Src(str(f)))
    assert seen == [f]
    assert out.kind == "unknown" and out.needs[0] == "unsupported format"
    assert out.needs[1].startswith("LibreOffice could not render it")


def _read_rows(tmp_path, monkeypatch, capsys, *paths):
    import json

    from meltify.cli import main

    monkeypatch.chdir(tmp_path)
    main(["read", *map(str, paths), "--json", "--limit", "0"])
    return {r["cite"]: r for r in json.loads(capsys.readouterr().out)["results"]}


def test_a_native_parser_that_gives_up_hands_the_file_to_the_renderer(
    tmp_path, monkeypatch, capsys
):
    pytest.importorskip("python_calamine")
    bad = tmp_path / "budget.xls"
    bad.write_bytes(b"not a workbook at all")
    _fake_soffice(monkeypatch)
    row = _read_rows(tmp_path, monkeypatch, capsys, bad)[str(bad)]
    assert row["kind"] == "rendered" and "error" not in row
    # Pages cite the original file, not the render
    assert f"## {bad}#p1\n" in Path(row["out"]).read_text()


def test_without_a_renderer_the_parse_error_is_a_need(tmp_path, monkeypatch, capsys):
    pytest.importorskip("python_calamine")
    bad = tmp_path / "budget.xls"
    bad.write_bytes(b"not a workbook at all")
    monkeypatch.setattr(render, "to_pdf", lambda path, timeout=0, out_dir=None: None)
    monkeypatch.setattr(render, "soffice", lambda: None)
    _no_media_or_preview(monkeypatch)
    row = _read_rows(tmp_path, monkeypatch, capsys, bad)[str(bad)]
    [need] = row["needs"]
    assert need.startswith("xls not read (") and need.endswith(f" ({LIBREOFFICE})")


def test_a_missing_extra_is_kept_next_to_the_render(tmp_path, monkeypatch, capsys):
    from meltify.converters import doc

    monkeypatch.setitem(sys.modules, "legacy_doc", None)
    monkeypatch.setattr(doc, "soffice", lambda: None)
    _fake_soffice(monkeypatch)
    doc = Path(__file__).parent / "fixtures" / "samples" / "legacy" / "SampleDoc.doc"
    row = _read_rows(tmp_path, monkeypatch, capsys, doc)[str(doc)]
    assert row["kind"] == "rendered" and row["needs"][-1] == "legacy-doc"


def test_encrypted_files_are_never_rendered(tmp_path, monkeypatch, capsys):
    import zipfile

    # Without it the file stops at the crypto extra's need before its verifier is read
    pytest.importorskip("cryptography")

    def refuse(path, timeout=0, out_dir=None):
        raise AssertionError("an encrypted file must not be rendered")

    monkeypatch.setattr(render, "to_pdf", refuse)
    _no_media_or_preview(monkeypatch)
    monkeypatch.setenv("MELTIFY_PASSWORD", "s3cr3t")
    # A locked Pages bundle whose verifier its converter can't parse
    locked = tmp_path / "plan.pages"
    with zipfile.ZipFile(locked, "w") as z:
        z.writestr(".iwph", "hint")
        z.writestr(".iwpv2", b"short")
        z.writestr("Index/Document.iwa", b"\0" * 64)
    row = _read_rows(tmp_path, monkeypatch, capsys, locked)[str(locked)]
    assert "iwpv2" in row["error"]


@pytest.mark.parametrize(
    "name, data",
    [
        ("drm.hwp", b"\x9b DRMONE" + bytes(64)),
        ("markany.doc", b"\x00\x01MarkAny wrapped" + bytes(64)),
    ],
)
def test_drm_wrappers_are_sealed(tmp_path, name, data):
    from meltify.converters import unlock

    path = tmp_path / name
    path.write_bytes(data)
    assert unlock.sealed(path)


LEGACY = Path(__file__).parent / "fixtures" / "samples" / "legacy"


def _resealed(source: Path, target: Path, stream: str, change) -> Path:
    """A copy of an OLE sample with one stream rewritten, like Office does when it encrypts"""
    from hwpx.hwp5.cfb import CompoundFile, build_compound_file, read_all_streams

    streams = read_all_streams(CompoundFile(source.read_bytes()))
    target.write_bytes(
        build_compound_file([(n, change(d) if n == stream else d) for n, d in streams])
    )
    return target


def _fib_encrypted(data: bytes) -> bytes:
    flags = int.from_bytes(data[10:12], "little") | 0x0100
    return data[:10] + flags.to_bytes(2, "little") + data[12:]


def _filepass_after_bof(data: bytes) -> bytes:
    size = int.from_bytes(data[2:4], "little")
    filepass = (0x002F).to_bytes(2, "little") + (6).to_bytes(2, "little") + bytes(6)
    return data[: 4 + size] + filepass + data[4 + size :]


def _ppt_token(data: bytes) -> bytes:
    return data[:12] + (0xF3D1C4DF).to_bytes(4, "little") + data[16:]


@pytest.mark.parametrize(
    ("sample", "stream", "change"),
    [
        ("SampleDoc.doc", "WordDocument", _fib_encrypted),
        ("SampleSS.xls", "Workbook", _filepass_after_bof),
        ("basic_test_ppt_file.ppt", "Current User", _ppt_token),
    ],
)
def test_only_encrypted_office_ole_counts_as_sealed_without_msoffcrypto(
    tmp_path, monkeypatch, sample, stream, change
):
    from meltify import passwords
    from meltify.converters import unlock

    monkeypatch.setitem(sys.modules, "msoffcrypto", None)
    plain = LEGACY / sample
    # A plain file whose parser failed still gets its render
    assert not unlock.sealed(plain)
    assert unlock.unlock(plain) == plain
    locked = _resealed(plain, tmp_path / sample, stream, change)
    assert unlock.sealed(locked)
    with pytest.raises(passwords.Locked, match="crypto extra"):
        unlock.unlock(locked)
    hwp = Path(__file__).parent / "fixtures" / "samples" / "docs" / "hwplib-table.hwp"
    assert not unlock.sealed(hwp)


def _hanging_tool(folder: Path, name: str, pid_file: Path) -> Path:
    # Starts a helper that holds no pipe, then hangs, like a Quick Look or LibreOffice launcher
    tool = folder / name
    tool.write_text(
        f"#!{sys.executable}\n"
        "import subprocess, sys, time\n"
        "w = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],\n"
        "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        f"open({str(pid_file)!r}, 'w').write(str(w.pid))\n"
        "time.sleep(60)\n"
    )
    tool.chmod(0o755)
    return tool


def _sites():
    from meltify.converters import archive, wordperfect

    return {
        "ffprobe": (fallback, "PROBE_TIMEOUT", lambda bin, p: fallback._media(p)),
        "textutil": (fallback, "SPOTLIGHT_TIMEOUT", lambda bin, p: fallback._spotlight_text(p)),
        "mdimport": (fallback, "SPOTLIGHT_TIMEOUT", lambda bin, p: fallback._spotlight_text(p)),
        "mdls": (quicklook, "MDLS_TIMEOUT", lambda bin, p: quicklook.previewable(p)),
        "wpd2text": (
            wordperfect,
            "TIMEOUT",
            lambda bin, p: wordperfect._wpd2text(str(bin / "wpd2text"), p),
        ),
        "7zz": (
            archive,
            "LIST_TIMEOUT",
            lambda bin, p: archive._sevenzip_list(str(bin / "7zz"), p, "secret"),
        ),
    }


@pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX")
@pytest.mark.parametrize("tool", ["ffprobe", "textutil", "mdimport", "mdls", "wpd2text", "7zz"])
def test_a_tool_that_times_out_takes_its_helpers_with_it(tmp_path, monkeypatch, tool):
    import os
    import time

    module, limit, call = _sites()[tool]
    bin = tmp_path / "bin"
    bin.mkdir()
    pid_file = tmp_path / "helper.pid"
    _hanging_tool(bin, tool, pid_file)
    monkeypatch.setenv("PATH", f"{bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(module, limit, 1)
    if tool == "mdimport":
        # textutil only reads the formats it knows, so this file goes straight to mdimport
        monkeypatch.setattr(shutil, "which", lambda name: str(bin / name) if name == tool else None)
        monkeypatch.setattr(quicklook, "previewable", lambda path: True)
    path = tmp_path / ("a.doc" if tool == "textutil" else "a.xyz")
    path.write_bytes(BINARY)
    with contextlib.suppress(subprocess.SubprocessError):
        call(bin, path)
    helper = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _alive(helper):
        time.sleep(0.05)
    assert not _alive(helper)


def _alive(pid: int) -> bool:
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class _Recorded:
    """Every program started, answered with empty output"""

    def __init__(self, monkeypatch):
        self.names: list[str] = []
        recorded = self

        class Popen:
            def __init__(self, args, **kwargs):
                recorded.names.append(Path(args[0]).name)
                self.args, self.pid, self.returncode = args, 4242, 0

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def communicate(self, input=None, timeout=None):
                return "", ""

            def wait(self, timeout=None):
                return 0

        def run(args, **kwargs):
            recorded.names.append(Path(args[0]).name)
            return subprocess.CompletedProcess(args, 0, "", "")

        monkeypatch.setattr(subprocess, "Popen", Popen)
        monkeypatch.setattr(subprocess, "run", run)
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")


ELF = b"\x7fELF\x02\x01\x01" + bytes(57)
PE = b"MZ" + bytes(58) + (64).to_bytes(4, "little") + b"PE\0\0" + bytes(60)
PYC = (3531).to_bytes(2, "little") + b"\r\n" + bytes(12) + b"\xe3" + bytes(40)


@pytest.mark.parametrize(
    "data",
    [ELF, b"\xcf\xfa\xed\xfe" + bytes(60), b"\xca\xfe\xba\xbe\x00\x00\x00\x41", PE, PYC,
     bytes(1 << 16)],
    ids=["elf", "macho", "class", "pe", "pyc", "zeros"],
)  # fmt: skip
def test_programs_and_filler_need_no_process(tmp_path, monkeypatch, data):
    calls = _Recorded(monkeypatch)
    f = tmp_path / "a.xyz"
    f.write_bytes(data)
    out = fallback.convert(f, Src(str(f)))
    assert out.kind == "unknown" and out.needs == ["unsupported format"]
    assert calls.names == []


def test_an_unknown_binary_asks_spotlight_its_type_once_and_skips_ffprobe(tmp_path, monkeypatch):
    calls = _Recorded(monkeypatch)
    f = tmp_path / "a.xyz"
    f.write_bytes(BINARY * 100)
    out = fallback.convert(f, Src(str(f)))
    assert out.needs == ["unsupported format"]
    # mdls says it isn't content, so neither Quick Look nor the Spotlight importer runs
    assert calls.names == ["mdls"]


@pytest.mark.parametrize(
    "head",
    [b"ID3\x04", b"\xff\xfb\x90\x00", b"\x00\x00\x00\x18ftypmp42", b"RIFF\x00\x00WAVE",
     b"\x1aE\xdf\xa3", b"OggS", b"\x30\x26\xb2\x75\x8e\x66\xcf\x11"],
)  # fmt: skip
def test_media_magic_still_reaches_ffprobe(tmp_path, monkeypatch, head):
    probed = []
    monkeypatch.setattr(fallback, "_media", lambda path: probed.append(path) or "audio")
    f = tmp_path / "clip.qqq"
    f.write_bytes(head + bytes(64))
    assert fallback.convert(f, Src(str(f))).kind == "media" and probed == [f]
