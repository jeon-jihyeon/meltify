import json

import pytest

from meltify.cli import main
from meltify.files import Pages
from meltify.forensics.pdf import DEFAULT_LIMITS, luminance, scan_page
from tests.fixtures.make_pdf import HIDDEN, VISIBLE, hidden_pdf, image_text_pdf, white_on_image_pdf


def scan(path, pages=None):
    """Every hidden span of the PDF at `path`, page by page"""
    import pymupdf

    with pymupdf.open(path) as doc:
        numbers = range(1, doc.page_count + 1) if pages is None else pages
        return [s for n in numbers for s in scan_page(doc[n - 1], n, DEFAULT_LIMITS)]


def test_finds_every_hiding_technique_without_false_positives(tmp_path):
    spans = scan(str(hidden_pdf(tmp_path / "h.pdf")))
    found = {s.text: s for s in spans}
    assert VISIBLE not in found
    assert set(found) == set(HIDDEN)
    for text, reason in HIDDEN.items():
        assert any(r.startswith(reason) for r in found[text].reasons), (text, found[text].reasons)


def test_luminance_handles_gray_rgb_and_cmyk():
    assert luminance(None) == 0
    assert luminance((1,)) == 1
    assert round(luminance((1, 1, 1)), 3) == 1
    assert round(luminance((0, 0, 0, 0)), 3) == 1


def _details(capsys, kind):
    out = json.loads(capsys.readouterr().out)
    return out, [r for r in out["results"] if r["kind"] == kind]


def test_read_hidden_lists_each_span_with_page_and_box(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    pdf = hidden_pdf(tmp_path / "h.pdf")
    assert main(["read", str(pdf), "--shallow", "--json"]) == 0
    out, spans = _details(capsys, "hidden")
    assert spans == [] and out["results"][0]["hidden"] == len(HIDDEN)
    assert main(["read", str(pdf), "--shallow", "--hidden", "--json"]) == 0
    out, spans = _details(capsys, "hidden")
    assert {r["text"] for r in spans} == set(HIDDEN)
    assert all(r["cite"].startswith(f"{pdf}#p1@pt(") for r in spans)
    assert all(r["out"] == out["results"][0]["out"] for r in spans)
    # Every span is listed, so nothing is left to follow up
    assert out["results"][0]["needs"] == []
    assert f"{len(HIDDEN)} hidden spans" in out["summary"]


def test_hidden_spans_of_a_nested_pdf_cite_through_the_archive(tmp_path, monkeypatch, capsys):
    import zipfile

    monkeypatch.chdir(tmp_path)
    pdf = hidden_pdf(tmp_path / "h.pdf")
    with zipfile.ZipFile(tmp_path / "a.zip", "w") as z:
        z.write(pdf, "docs/h.pdf")
    assert main(["read", "a.zip", "--shallow", "--hidden", "--json"]) == 0
    _, spans = _details(capsys, "hidden")
    assert len(spans) == len(HIDDEN)
    assert all(r["cite"].startswith("a.zip#att=docs/h.pdf#p1@pt(") for r in spans)


def test_contrast_renders_pages_for_text_inside_images(tmp_path, monkeypatch, capsys):
    import numpy as np
    from PIL import Image

    monkeypatch.chdir(tmp_path)
    pdf = image_text_pdf(tmp_path / "img.pdf")
    assert main(["read", str(pdf), "--shallow", "--hidden", "--contrast", "--json"]) == 0
    out, renders = _details(capsys, "contrast")
    assert [r["cite"] for r in renders] == [f"{pdf}#p1"]
    assert _details_of(out, "hidden") == []
    image = Image.open(renders[0]["path"]).convert("L")
    # The faint text becomes visible as a wide spread of tones
    assert np.asarray(image).std() > 20
    assert "/attachments/img.pdf/contrast/" in renders[0]["path"]


def _details_of(out, kind):
    return [r for r in out["results"] if r["kind"] == kind]


def test_pages_parse_and_select():
    assert Pages.parse("1,3-5").of(6) == [1, 3, 4, 5]
    assert Pages.parse("2,2").of(3) == [2]
    # A range past the end selects what's there, since read applies it to every PDF
    assert Pages.parse("2-9").of(3) == [2, 3]


@pytest.mark.parametrize("spec", ["0", "5-3", "1,", "", "a", "1-"])
def test_pages_reject_bad_specs(spec):
    with pytest.raises(ValueError):
        Pages.parse(spec)


def test_pages_limit_what_read_melts_and_scans(tmp_path, monkeypatch, capsys):
    import pymupdf

    monkeypatch.chdir(tmp_path)
    doc = pymupdf.open()
    for n in range(3):
        doc.new_page().insert_text((72, 72), f"page {n + 1} text")
    doc.save(tmp_path / "three.pdf")
    assert main(["read", "three.pdf", "--pages", "2-3", "--json"]) == 0
    md = (tmp_path / "meltify-out/read/three.pdf.md").read_text()
    assert "page 1 text" not in md and "page 2 text" in md and "page 3 text" in md
    capsys.readouterr()
    assert main(["read", "three.pdf", "--pages", "7", "--json"]) == 0
    [row] = json.loads(capsys.readouterr().out)["results"]
    assert row["needs"] == ["no pages in 7, it has 3"]
    with pytest.raises(SystemExit):
        main(["read", "three.pdf", "--pages", "2-1"])


def test_white_text_over_dark_image_is_not_hidden(tmp_path):
    assert scan(str(white_on_image_pdf(tmp_path / "w.pdf"))) == []


def _reference_scan(page, number):
    """The plain pass over the whole draw log per span that scan_page must match"""
    import pymupdf

    from meltify.forensics.pdf import COVERING, DEFAULT_LIMITS, luminance

    log = page.get_bboxlog()
    fills = [(d["seqno"], d["rect"], d["fill"]) for d in page.get_drawings() if d.get("fill")]
    found = []
    for s in page.get_texttrace():
        text = "".join(chr(ch[0]) for ch in s["chars"]).strip()
        if not text:
            continue
        rect, seq = pymupdf.Rect(s["bbox"]), s["seqno"]
        behind = [(q, fill) for q, r, fill in fills if q < seq and r.contains(rect)]
        bg = luminance(behind[-1][1]) if behind else 1.0
        images = [
            q
            for q, (kind, box, *_) in enumerate(log[:seq])
            if kind == "fill-image" and pymupdf.Rect(box).intersects(rect)
        ]
        over_image = bool(images) and (not behind or images[-1] > behind[-1][0])
        fg = luminance(s["color"])
        reasons = []
        if s["type"] == 3:
            reasons.append("render mode 3")
        if s["opacity"] < DEFAULT_LIMITS.opacity:
            reasons.append(f"opacity {s['opacity']:.2f}")
        if s["size"] < DEFAULT_LIMITS.size:
            reasons.append(f"size {s['size']:.1f}pt")
        if not rect.intersects(page.rect):
            reasons.append("off page")
        if not over_image and abs(fg - bg) < DEFAULT_LIMITS.contrast:
            reasons.append(f"color matches background by {abs(fg - bg):.2f}")
        if any(
            kind in COVERING and pymupdf.Rect(box).contains(rect)
            for kind, box, *_ in log[seq + 1 :]
        ):
            reasons.append("covered by a later fill")
        if reasons:
            found.append((number, text, tuple(reasons), tuple(round(v, 1) for v in rect)))
    return found


def _layered_pdf(path):
    """Text, fills and images interleaved, so spans sit over, under and beside each kind"""
    import io

    import pymupdf
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (60, 40), (30, 30, 30)).save(buf, format="PNG")
    doc = pymupdf.open()
    page = doc.new_page()
    for i in range(80):
        x, y = 40 + (i * 37) % 480, 40 + (i * 53) % 700
        if i % 5 == 0:
            page.draw_rect(pymupdf.Rect(x - 20, y - 20, x + 120, y + 30), fill=(0.9, 0.9, 0.9))
        if i % 7 == 0:
            page.insert_image(pymupdf.Rect(x - 10, y - 15, x + 90, y + 15), stream=buf.getvalue())
        gray = (i % 4) / 3
        page.insert_text((x, y), f"span {i}", fontsize=3 + i % 9, color=(gray,) * 3)
        if i % 11 == 0:
            page.insert_text((x, y + 8), f"mode {i}", fontsize=10, render_mode=3)
    page.insert_text((60, 2000), "far below", fontsize=10)
    doc.save(path)
    return path


@pytest.mark.parametrize("make", [hidden_pdf, white_on_image_pdf, image_text_pdf, _layered_pdf])
def test_scan_matches_a_plain_pass_over_the_draw_log(tmp_path, make):
    import pymupdf

    path = make(tmp_path / "p.pdf")
    with pymupdf.open(path) as doc:
        expected = [hit for n, page in enumerate(doc, start=1) for hit in _reference_scan(page, n)]
    got = [(s.page, s.text, s.reasons, s.bbox) for s in scan(str(path))]
    assert got == expected


def test_page_without_text_skips_the_vector_pass(tmp_path, monkeypatch):
    import pymupdf

    doc = pymupdf.open()
    doc.new_page().draw_rect(pymupdf.Rect(10, 10, 100, 100), fill=(0.5, 0.5, 0.5))
    doc.save(tmp_path / "v.pdf")

    def fail(*_, **__):
        raise AssertionError("read the drawings of a page with no text")

    monkeypatch.setattr(pymupdf.Page, "get_cdrawings", fail)
    monkeypatch.setattr(pymupdf.Page, "get_drawings", fail)
    assert scan(str(tmp_path / "v.pdf")) == []


def test_pdf_read_traces_each_page_once(tmp_path, monkeypatch):
    import pymupdf

    from meltify.converters import pdf
    from meltify.evidence import Src
    from tests.fixtures.make_docs import card

    doc = pymupdf.open()
    for n in range(3):
        # A different scan on each page, since one repeated on most pages counts as a logo
        page = doc.new_page()
        page.insert_image(page.rect, stream=card(f"SCANNED BODY {n}", (850, 1100)))
        page.insert_text((72, 300), f"SCANNED BODY {n} as the scanner read it", render_mode=3)
    doc.save(tmp_path / "s.pdf")
    traced = []
    original = pymupdf.Page.get_texttrace
    monkeypatch.setattr(
        pymupdf.Page, "get_texttrace", lambda page: traced.append(page.number) or original(page)
    )
    out = pdf.convert(tmp_path / "s.pdf", Src("s.pdf"))
    # The scan layer check and the hidden text check share one trace per page
    assert sorted(traced) == [0, 1, 2]
    assert out.jobs == [] and out.hidden == 3


def test_the_old_command_still_runs_with_a_warning(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    pdf = hidden_pdf(tmp_path / "h.pdf")
    assert main(["hidden", str(pdf), "--json"]) == 0
    out, spans = _details(capsys, "hidden")
    assert out["command"] == "read" and len(spans) == len(HIDDEN)
    assert out["warnings"][0] == (
        "meltify hidden is deprecated and will be removed in 0.4.0,"
        " use meltify read --hidden instead"
    )


def test_hidden_says_what_it_left_unchecked(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "notes.md").write_text("plain\n")
    image_text_pdf(tmp_path / "img.pdf")
    assert main(["read", "notes.md", "img.pdf", "--shallow", "--hidden", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["summary"].endswith(", 0 hidden spans")
    assert "--hidden checks PDFs and SVGs only, so 1 other item went unchecked" in out["warnings"]
    assert any("add --contrast" in w for w in out["warnings"])


def test_the_table_shows_what_hidden_rows_found(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    hidden_pdf(tmp_path / "h.pdf")
    assert main(["read", "h.pdf", "--shallow", "--hidden", "--contrast"]) == 0
    header = capsys.readouterr().out.splitlines()[0].split()
    assert header == ["kind", "chars", "needs", "out", "text", "reasons", "path", "cite"]
