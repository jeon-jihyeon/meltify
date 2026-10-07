import io
import json
import struct
import sys
import zipfile
from pathlib import Path

import pytest

from meltify import passwords
from meltify.cli import main
from meltify.converters import blips, doc, metafile, pick, ppt, quicklook, render, unlock
from meltify.converters.blips import PICTURE_ENTRY, SHAPE_OPTIONS
from meltify.converters.embeds import DRAW_HINT
from meltify.converters.ooxml import XLSB_MAIN
from meltify.converters.run import LOCK, RunContext
from meltify.evidence import Src
from meltify.needs import LIBREOFFICE
from tests.fixtures.make_docs import card, word97

SAMPLES = Path(__file__).parent / "fixtures" / "samples" / "legacy"
DOC = SAMPLES / "SampleDoc.doc"
XLS = SAMPLES / "SampleSS.xls"
PPT = SAMPLES / "basic_test_ppt_file.ppt"


def _rows(tmp_path, monkeypatch, capsys, *paths):
    monkeypatch.chdir(tmp_path)
    main(["read", *map(str, paths), "--json", "--limit", "0"])
    out = json.loads(capsys.readouterr().out)
    return {r["cite"]: r for r in out["results"]}


def _soffice(monkeypatch, found):
    for module in (doc, ppt):
        monkeypatch.setattr(module, "soffice", found)


def _convert(path, src):
    return pick(Path(path))[1](Path(path), src)


def _no_renderers(monkeypatch):
    # Without LibreOffice or Quick Look, read has no render to fall back on
    _soffice(monkeypatch, lambda: None)
    monkeypatch.setattr(render, "soffice", lambda: None)
    monkeypatch.setattr(quicklook, "available", lambda: False)


def _md(row):
    return Path(row["out"]).read_text(encoding="utf-8")


def test_doc_cites_lines(tmp_path, monkeypatch, capsys):
    pytest.importorskip("legacy_doc")
    row = _rows(tmp_path, monkeypatch, capsys, DOC)[str(DOC)]
    assert row["kind"] == "legacy" and row["needs"] == []
    md = _md(row)
    assert f"## {DOC}:1\n1| I am a test document" in md
    assert "4| This is page two" in md


def test_xls_cites_sheets_like_xlsx(tmp_path, monkeypatch, capsys):
    pytest.importorskip("python_calamine")
    md = _md(_rows(tmp_path, monkeypatch, capsys, XLS)[str(XLS)])
    assert f"## {XLS}#First Sheet\n| row | A | B |" in md
    # Numbers come back as ints, so B7 reads 10 like openpyxl would show it
    assert f"## {XLS}#Sheet Number 2" in md
    assert "| 7 | 1 | 10 | 2 | 13 |" in md
    assert "| 3 |" not in md


def test_ppt_slides_cite_the_slide_without_libreoffice(tmp_path, monkeypatch, capsys):
    pytest.importorskip("olefile")
    _soffice(monkeypatch, lambda: None)
    row = _rows(tmp_path, monkeypatch, capsys, PPT)[str(PPT)]
    assert row["kind"] == "legacy" and row["needs"] == []
    md = _md(row)
    assert f"## {PPT}#slide1\nThis is a test title\nThis is a test subtitle" in md
    assert f"## {PPT}#slide2\nThis is the title on page 2" in md
    assert "Notes:\nThese are the notes on page two, again lacking formatting" in md


def test_doc_without_legacy_doc_or_soffice_reports_the_extra(tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "legacy_doc", None)
    _no_renderers(monkeypatch)
    row = _rows(tmp_path, monkeypatch, capsys, DOC)[str(DOC)]
    assert row["needs"] == ["legacy-doc"]
    assert row["hint"] == "meltify doctor --install office"


def test_unreadable_doc_points_at_libreoffice(tmp_path, monkeypatch):
    pytest.importorskip("legacy_doc")
    _soffice(monkeypatch, lambda: None)
    bad = tmp_path / "broken.doc"
    bad.write_bytes(b"not an OLE file")
    out = _convert(bad, Src(str(bad)))
    assert out.blocks == []
    assert len(out.needs) == 1 and out.needs[0].endswith(f"{LIBREOFFICE} to retry)")


@pytest.mark.parametrize("name", ["memo.doc", "memo.dot"])
def test_doc_inline_and_floating_pictures_go_to_ocr(tmp_path, name):
    pytest.importorskip("olefile")
    inline, floating = card("INLINE PICTURE"), card("FLOATING PICTURE")
    path = word97(tmp_path / name, "Intro\r\x01\rAfter\x08 text\rEnd\r", inline, floating)
    out = _convert(path, Src(name))
    assert [(j.src.cite(), j.data) for j in out.jobs] == [
        (f"{name}#para2#img1", inline),
        (f"{name}#para3#img2", floating),
    ]
    assert out.needs == []


def test_doc_pictures_that_cant_be_read_are_a_need(tmp_path, monkeypatch):
    pytest.importorskip("olefile")
    path = word97(tmp_path / "memo.doc", "Intro\r\x01\r\x08\r", card("A"), card("B"))

    def broken(*args):
        raise doc.DocError("no piece table")

    monkeypatch.setattr(doc, "_pieces", broken)
    out = _convert(path, Src("memo.doc"))
    assert out.jobs == [] and out.needs == ["doc pictures not read (DocError: no piece table)"]


REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"


def _record(kind: int, body: bytes = b"") -> bytes:
    """One BIFF12 record, type and size each written as 7-bit groups"""

    def varint(n: int) -> bytes:
        out = bytearray()
        while True:
            out.append((n & 0x7F) | (0x80 if n >> 7 else 0))
            n >>= 7
            if not n:
                return bytes(out)

    return varint(kind) + varint(len(body)) + body


def _wide(text: str) -> bytes:
    return struct.pack("<I", len(text)) + text.encode("utf-16-le")


def _xlsb(path: Path, sheets: dict[str, list[list]], media: dict[str, bytes]) -> Path:
    """The smallest xlsb calamine accepts: a workbook, sheet rels and cell records"""
    book = _record(131) + _record(143)
    rels = ""
    for i, name in enumerate(sheets, start=1):
        book += _record(156, struct.pack("<II", 0, i) + _wide(f"rId{i}") + _wide(name))
        target = f"worksheets/sheet{i}.bin"
        rels += f'<Relationship Id="rId{i}" Type="{REL}/worksheet" Target="{target}"/>'
    book += _record(144) + _record(132)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            f'<Override PartName="/xl/workbook.bin" ContentType="{XLSB_MAIN}"/></Types>',
        )
        z.writestr(
            "_rels/.rels",
            f'<Relationships xmlns="{PKG}"><Relationship Id="rId1" Type="{REL}/officeDocument"'
            ' Target="xl/workbook.bin"/></Relationships>',
        )
        z.writestr(
            "xl/_rels/workbook.bin.rels", f'<Relationships xmlns="{PKG}">{rels}</Relationships>'
        )
        z.writestr("xl/workbook.bin", book)
        for i, rows in enumerate(sheets.values(), start=1):
            dim = struct.pack("<IIII", 0, len(rows) - 1, 0, max(map(len, rows)) - 1)
            body = _record(129) + _record(148, dim) + _record(145)
            for r, row in enumerate(rows):
                body += _record(0, struct.pack("<IIHHBBI", r, 0, 300, 0, 0, 0, 0))
                for c, v in enumerate(row):
                    head = struct.pack("<II", c, 0)
                    if isinstance(v, float):
                        body += _record(5, head + struct.pack("<d", v))
                    else:
                        body += _record(6, head + _wide(v))
            z.writestr(f"xl/worksheets/sheet{i}.bin", body + _record(146) + _record(130))
        for name, data in media.items():
            z.writestr(name, data)
    return path


def _png() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (120, 80), "white").save(buf, "PNG")
    return buf.getvalue()


def test_xlsb_cites_cells_like_xls(tmp_path):
    pytest.importorskip("python_calamine")
    path = _xlsb(
        tmp_path / "budget.xlsb",
        {"Budget": [["item", "cost"], ["paper", 12.0], ["잉크", 7.5]], "Notes": [["ok"]]},
        {},
    )
    out = _convert(path, Src(str(path)))
    assert [b.src.cite() for b in out.blocks] == [f"{path}#Budget", f"{path}#Notes"]
    assert out.blocks[0].text.splitlines()[2:] == [
        "| 1 | item | cost |",
        "| 2 | paper | 12 |",
        "| 3 | 잉크 | 7.5 |",
    ]
    assert out.needs == [] and out.jobs == []


C = "http://schemas.openxmlformats.org/drawingml/2006/chart"


def _chart(values: str) -> bytes:
    return (
        f'<c:chartSpace xmlns:c="{C}"><c:chart><c:plotArea><c:barChart><c:ser>'
        "<c:cat><c:strRef><c:f>S!$A$1:$A$2</c:f></c:strRef></c:cat>"
        f"<c:val><c:numRef><c:f>S!$B$1:$B$2</c:f>{values}</c:numRef></c:val>"
        "</c:ser></c:barChart></c:plotArea></c:chart></c:chartSpace>"
    ).encode()


def test_xlsb_pictures_become_jobs_and_charts_read_their_cells(tmp_path, monkeypatch):
    pytest.importorskip("python_calamine")
    monkeypatch.setattr(render, "available", lambda: False)
    monkeypatch.setattr(metafile, "replay_available", lambda: False)
    cached = "<c:numCache><c:pt idx='0'><c:v>5</c:v></c:pt></c:numCache>"
    media = {
        "xl/media/image1.png": _png(),
        "xl/media/image2.emf": b"\x01\x00\x00\x00",
        "xl/charts/chart1.xml": _chart(cached),
        # No cache, so the formula reads the binary sheet's cells instead
        "xl/charts/chart2.xml": _chart(""),
        "xl/charts/chart3.xml": b"<broken",
    }
    path = _xlsb(tmp_path / "pics.xlsb", {"S": [["kiwi", 3.0], ["fig", 4.0]]}, media)
    out = _convert(path, Src(str(path)))
    assert [j.src.cite() for j in out.jobs] == [f"{path}#img1"]
    charts = [(b.src.cite(), b.text.splitlines()[3:]) for b in out.blocks[1:]]
    assert charts == [
        (f"{path}#img3", ["| kiwi | 5 |", "| fig |  |"]),
        (f"{path}#img4", ["| kiwi | 3 |", "| fig | 4 |"]),
    ]
    assert sorted(out.needs) == [
        f"1 emf image not read ({DRAW_HINT})",
        "1 unreadable chart not read",
    ]


def test_xlsb_under_any_name_is_read_as_a_workbook(tmp_path):
    from meltify.converters import pick

    pytest.importorskip("python_calamine")
    path = _xlsb(tmp_path / "book_download", {"S": [["kiwi", 3.0]]}, {})
    kind, convert = pick(path)
    out = convert(path, Src(str(path)))
    assert kind == "legacy"
    assert out.blocks[0].src.cite() == f"{path}#S"
    assert "| 1 | kiwi | 3 |" in out.blocks[0].text


def test_xlsb_svg_goes_on_to_the_svg_converter(tmp_path, monkeypatch):
    pytest.importorskip("python_calamine")
    svg = b"<svg><text>Org chart</text></svg>"
    path = _xlsb(tmp_path / "s.xlsb", {"S": [["a", 1.0]]}, {"xl/media/image1.svg": svg})
    out = _convert(path, Src("s.xlsb"))
    assert [(c.name, c.data) for c in out.children] == [("xl/media/image1.svg", svg)]
    assert out.needs == []


def test_ppt_holds_the_pymupdf_lock_only_while_reading_the_render(tmp_path, monkeypatch):
    import threading

    from meltify.converters import pdf

    def free() -> bool:
        # The lock is reentrant, so only another thread can tell whether it's held
        got: list[bool] = []

        def probe() -> None:
            got.append(LOCK.acquire(blocking=False))
            if got[0]:
                LOCK.release()

        t = threading.Thread(target=probe)
        t.start()
        t.join()
        return got[0]

    seen = {}

    def run(args, *, timeout=None, check=True):
        seen["soffice"] = free()
        out = Path(args[args.index("--outdir") + 1])
        (out / f"{Path(args[-1]).stem}.pdf").write_bytes(b"%PDF-")

    def melt(path, src, look):
        seen["pymupdf"] = free()
        return ppt.Converted("pdf")

    _soffice(monkeypatch, lambda: "/opt/soffice")
    monkeypatch.setattr(render, "soffice", lambda: "/opt/soffice")
    monkeypatch.setattr(render, "run", run)
    monkeypatch.setattr(pdf, "melt", melt)
    out = _convert(tmp_path / "deck.pps", Src("deck.pps"))
    assert out.kind == "legacy"
    assert seen == {"soffice": True, "pymupdf": False}


def test_doc_and_ppt_go_through_the_one_soffice_helper(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "legacy_doc", None)
    calls = []

    def run(args, *, timeout=None, check=True):
        calls.append(args)
        out = Path(args[args.index("--outdir") + 1])
        (out / f"{Path(args[-1]).stem}.txt").write_text("first line\nsecond line")

    _soffice(monkeypatch, lambda: "/opt/soffice")
    monkeypatch.setattr(render, "soffice", lambda: "/opt/soffice")
    monkeypatch.setattr(render, "run", run)
    out = _convert(tmp_path / "memo.doc", Src("memo.doc"))
    assert [b.text for b in out.blocks] == ["1| first line\n2| second line"]
    [args] = calls
    assert "--norestore" in args
    assert args[args.index("--convert-to") + 1] == "txt:Text (encoded):UTF8"


def _ppt_record(kind: int, body: bytes = b"", inst: int = 0, container: bool = False) -> bytes:
    """One MS-PPT or OfficeArt record, a container when it holds other records"""
    ver = 0xF if container else 0
    return struct.pack("<HHI", (inst << 4) | ver, kind, len(body)) + body


def _chars(text: str) -> bytes:
    return _ppt_record(ppt.TEXT_CHARS, text.encode("utf-16-le"))


def _slide(*records: bytes, hidden: bool = False) -> bytes:
    """A slide container whose drawing holds the given shape records"""
    show = _ppt_record(ppt.SLIDE_SHOW_INFO, struct.pack("<IIBBH4x", 0, 0, 0, 0, 4 * hidden))
    textbox = _ppt_record(0xF00D, b"".join(records), container=True)
    shape = _ppt_record(0xF004, textbox, container=True)
    drawing = _ppt_record(0x040C, _ppt_record(0xF002, shape, container=True), container=True)
    return _ppt_record(ppt.SLIDE, show + drawing, container=True)


def _picture_ref(index: int) -> bytes:
    # pib with the fBid bit, so the value is a 1-based picture store index
    return _ppt_record(SHAPE_OPTIONS, struct.pack("<HI", 0x4104, index), inst=1)


def _deck_file(
    path: Path,
    slides: list[bytes],
    outline: dict[int, list[str]] | None = None,
    notes: dict[int, str] | None = None,
    pictures: list[bytes] = (),
    token: int = ppt.PLAIN,
    extra: dict | None = None,
    repeat: int = 1,
) -> Path:
    """A PowerPoint 97 file: slides, notes pages and a picture store, in one save

    `repeat` gives each picture that many store entries, all pointing at its one record
    """
    from tests.fixtures.make_binary import compound_file

    outline, notes = outline or {}, notes or {}
    stream = bytearray()
    offsets: dict[int, int] = {}

    def put(ref: int, data: bytes) -> None:
        offsets[ref] = len(stream)
        stream.extend(data)

    # Persist ids: 1 document, 2.. slides, then notes pages
    slide_list = b""
    for n, _ in enumerate(slides):
        slide_list += _ppt_record(ppt.SLIDE_PERSIST, struct.pack("<IIiII", n + 2, 0, 0, 256 + n, 0))
        for text in outline.get(n + 1, []):
            slide_list += _ppt_record(ppt.TEXT_BYTES, text.encode("latin-1"))
    note_list = b""
    note_refs = {}
    for k, n in enumerate(notes, start=len(slides) + 2):
        note_refs[n] = k
        note_list += _ppt_record(ppt.SLIDE_PERSIST, struct.pack("<IIiII", k, 0, 0, 0, 0))
    store = b""
    blips = b""
    for blip in pictures:
        entry = struct.pack(
            "<BB16sHIIIBBBB", 6, 6, b"\0" * 16, 0, len(blip), 1, len(blips), 0, 0, 0, 0
        )
        store += _ppt_record(PICTURE_ENTRY, entry, inst=6) * repeat
        blips += blip
    group = _ppt_record(
        0x040B,
        _ppt_record(0xF000, _ppt_record(0xF001, store, container=True), container=True),
        container=True,
    )
    document = _ppt_record(
        ppt.DOCUMENT,
        _ppt_record(ppt.SLIDE_LIST, slide_list, inst=0, container=True)
        + _ppt_record(ppt.SLIDE_LIST, note_list, inst=2, container=True)
        + group,
        container=True,
    )
    put(1, document)
    for n, slide in enumerate(slides):
        put(n + 2, slide)
    for n, text in notes.items():
        atom = _ppt_record(ppt.NOTES_ATOM, struct.pack("<IHH", 256 + n - 1, 0, 0))
        put(note_refs[n], _ppt_record(ppt.NOTES, atom + _slide(_chars(text))[8:], container=True))
    refs = sorted(offsets)
    directory_at = len(stream)
    entries = struct.pack("<I", refs[0] | (len(refs) << 20))
    entries += b"".join(struct.pack("<I", offsets[r]) for r in refs)
    stream += _ppt_record(ppt.PERSIST_DIRECTORY, entries)
    edit_at = len(stream)
    edit = struct.pack("<IHBBIIIIHH", 256, 0, 0, 3, 0, directory_at, 1, max(refs) + 1, 1, 0)
    stream += _ppt_record(ppt.USER_EDIT, edit)
    user = _ppt_record(
        ppt.CURRENT_USER, struct.pack("<IIIHHBBH", 0x14, token, edit_at, 0, 0x03F4, 3, 0, 0)
    )
    tree = {"Current User": user, "PowerPoint Document": bytes(stream), **(extra or {})}
    if blips:
        tree["Pictures"] = blips
    path.write_bytes(compound_file(tree))
    return path


def _png_blip(data: bytes) -> bytes:
    # One 16 byte id and a tag byte before the file
    return _ppt_record(0xF01E, b"\0" * 16 + b"\xff" + data, inst=0x6E0)


def _emf_blip(data: bytes) -> bytes:
    # A metafile header with compression 0xFE, stored as is
    header = struct.pack("<I16s8sIBB", len(data), b"", b"", len(data), 0xFE, 0xFE)
    return _ppt_record(0xF01A, b"\0" * 16 + header + data, inst=0x3D4)


def _sample_deck(path: Path) -> Path:
    return _deck_file(
        path,
        [
            # The title placeholder points at the slide list's outline text
            _slide(
                _ppt_record(ppt.OUTLINE_REF, struct.pack("<i", 0)),
                _chars("매출 12% 증가\rQ3 넘어"),
                _chars("*"),
                _picture_ref(1),
            ),
            _slide(),
        ],
        outline={1: ["Quarterly plan"]},
        notes={1: "Speaker note"},
        pictures=[_png_blip(card("Q3 CHART 4,210", (300, 100)))],
    )


def test_ppt_reads_slides_notes_and_pictures_natively(tmp_path, monkeypatch):
    pytest.importorskip("olefile")
    _soffice(monkeypatch, lambda: None)
    path = _sample_deck(tmp_path / "plan.ppt")
    out = _convert(path, Src("plan.ppt"))
    assert [(b.src.cite(), b.text) for b in out.blocks] == [
        ("plan.ppt#slide1", "Quarterly plan\n매출 12% 증가\nQ3 넘어\n\nNotes:\nSpeaker note")
    ]
    assert [j.src.cite() for j in out.jobs] == ["plan.ppt#slide1#img1"]
    assert out.jobs[0].data.startswith(b"\x89PNG")
    assert out.needs == [f"1 slide without text not read ({LIBREOFFICE})"]


def test_ppt_renders_only_the_slides_without_text(tmp_path, monkeypatch):
    pytest.importorskip("olefile")
    path = _deck_file(
        tmp_path / "d.ppt",
        [_slide(_chars("Intro")), _slide(hidden=True), _slide(), _slide()],
    )
    rendered = []

    def to_pdf(src, target, out_dir, timeout):
        pdf = out_dir / "d.pdf"
        pdf.write_bytes(b"%PDF-")
        return pdf

    def png(pdf, page=1, clip=None, dpi=200):
        rendered.append(page)
        if page == 3:
            raise RuntimeError("no such page")
        return b"png of page %d" % page

    _soffice(monkeypatch, lambda: "/opt/soffice")
    monkeypatch.setattr(render, "convert_to", to_pdf)
    monkeypatch.setattr(render, "png", png)
    out = _convert(path, Src("d.ppt"))
    # The hidden slide has no page, so slide 3 is page 2 of the render
    assert rendered == [2, 3]
    assert [(j.src.cite(), j.data) for j in out.jobs] == [("d.ppt#slide3", b"png of page 2")]
    assert out.needs == [
        "1 hidden slide without text not read (a PDF render leaves it out)",
        "1 slide without text not read (LibreOffice could not render it)",
    ]


def _stamped_pdfs(monkeypatch) -> None:
    """soffice standing in for every render path, stamping each PDF with when it was made"""
    import pymupdf

    made = iter(range(100))

    def convert_to(path, target, out_dir, timeout):
        out_dir.mkdir(parents=True, exist_ok=True)
        doc = pymupdf.open()
        doc.new_page()
        doc.set_metadata({"creationDate": f"D:2026010{next(made)}"})
        doc.save(out_dir / f"{path.stem}.pdf")
        return out_dir / f"{path.stem}.pdf"

    monkeypatch.setattr(render, "soffice", lambda: "/opt/soffice")
    monkeypatch.setattr(render, "convert_to", convert_to)
    monkeypatch.setattr(doc, "convert_to", convert_to)


def test_rendered_ppt_pages_key_on_the_ppt_not_the_render(tmp_path, monkeypatch):
    from meltify.recognize import Options, Recognizer

    _stamped_pdfs(monkeypatch)
    path = tmp_path / "old.ppt"
    path.write_bytes(b"PowerPoint 95 deck")
    runs = [ppt._rendered(path, Src("old.ppt")) for _ in range(2)]
    [first], [second] = (out.jobs for out in runs)
    assert first.path.read_bytes() != second.path.read_bytes()
    reader = Recognizer(Options(), {}, print)
    # The OCR cache hits on the second run, since both renders drew the same ppt
    assert reader.key(first) == reader.key(second)


def test_shallow_ppt_renders_no_empty_slide(tmp_path, monkeypatch):

    pytest.importorskip("olefile")
    path = _deck_file(tmp_path / "d.ppt", [_slide(_chars("Intro")), _slide(), _slide()])
    _soffice(monkeypatch, lambda: "/opt/soffice")
    monkeypatch.setattr(render, "soffice", lambda: "/opt/soffice")
    monkeypatch.setattr(render, "run", lambda *a, **k: pytest.fail("started soffice"))
    monkeypatch.setattr("meltify.converters.run.current", lambda: RunContext(shallow=True))
    out = _convert(path, Src("d.ppt"))
    assert out.jobs == []
    assert out.needs == ["2 slides without text not read (not drawn under --shallow)"]


def test_ppt_metafiles_wait_for_libreoffice(tmp_path, monkeypatch):
    pytest.importorskip("olefile")
    _soffice(monkeypatch, lambda: None)
    monkeypatch.setattr(render, "available", lambda: False)
    monkeypatch.setattr(metafile, "replay_available", lambda: False)
    emf = b"\x01\x00\x00\x00" + b"\0" * 60
    path = _deck_file(
        tmp_path / "m.ppt", [_slide(_chars("Logo"), _picture_ref(1))], pictures=[_emf_blip(emf)]
    )
    out = _convert(path, Src("m.ppt"))
    assert out.jobs == []
    assert out.needs == [f"1 emf image not read ({DRAW_HINT})"]


def test_password_protected_ppt_is_a_need(tmp_path, monkeypatch):
    pytest.importorskip("olefile")
    path = _deck_file(tmp_path / "s.ppt", [_slide(_chars("secret"))], token=ppt.ENCRYPTED)
    monkeypatch.setitem(sys.modules, "msoffcrypto", None)
    with pytest.raises(passwords.Locked) as e:
        _convert(path, Src("s.ppt"))
    assert str(e.value) == unlock.NEEDS_CRYPTO


def _fake_ppt97(monkeypatch, plain: bytes, secret: str) -> list[str]:
    """msoffcrypto's ppt reader, whose load_key takes only the password and checks it itself"""
    from msoffcrypto.exceptions import InvalidKeyError

    tried: list[str] = []

    class Ppt97File:
        def __init__(self, f):
            pass

        def is_encrypted(self):
            return True

        def load_key(self, password=None):
            tried.append(password)
            if password != secret:
                raise InvalidKeyError("Failed to verify password")

        def decrypt(self, out):
            out.write(plain)

    monkeypatch.setattr("msoffcrypto.OfficeFile", Ppt97File)
    return tried


def test_password_protected_ppt_opens_through_unlock(tmp_path, monkeypatch):
    pytest.importorskip("olefile")
    pytest.importorskip("msoffcrypto")
    locked = _deck_file(tmp_path / "s.ppt", [_slide(_chars("x"))], token=ppt.ENCRYPTED)
    plain = _deck_file(tmp_path / "plain.ppt", [_slide(_chars("Merger terms"))]).read_bytes()
    tried = _fake_ppt97(monkeypatch, plain, "hunter2")
    with pytest.raises(passwords.Locked) as e:
        _convert(locked, Src("s.ppt"))
    assert str(e.value) == passwords.LOCKED
    monkeypatch.setenv("MELTIFY_PASSWORD", "hunter2")
    out = _convert(locked, Src("s.ppt"))
    assert [(b.src.cite(), b.text) for b in out.blocks] == [("s.ppt#slide1", "Merger terms")]
    assert tried == ["hunter2"] and out.needs == []
    monkeypatch.setenv("MELTIFY_PASSWORD", "wrong")
    with pytest.raises(passwords.Locked) as e:
        _convert(locked, Src("s.ppt"))
    assert str(e.value) == passwords.WRONG


def test_powerpoint_95_is_a_need_or_goes_to_libreoffice(tmp_path, monkeypatch):
    from tests.fixtures.make_binary import compound_file

    pytest.importorskip("olefile")
    path = tmp_path / "old.ppt"
    path.write_bytes(compound_file({"PP40": b"\0" * 64}))
    _soffice(monkeypatch, lambda: None)
    out = _convert(path, Src("old.ppt"))
    assert out.needs == [f"PowerPoint 95 ppt not read ({LIBREOFFICE})"]
    _soffice(monkeypatch, lambda: "/opt/soffice")
    monkeypatch.setattr(ppt, "_rendered", lambda p, s: ppt.Converted("legacy", needs=["r"]))
    assert _convert(path, Src("old.ppt")).needs == ["r"]


def test_broken_ppt_without_libreoffice_says_why(tmp_path, monkeypatch):
    pytest.importorskip("olefile")
    _soffice(monkeypatch, lambda: None)
    bad = tmp_path / "broken.ppt"
    bad.write_bytes(b"not an OLE file")
    out = _convert(bad, Src("broken.ppt"))
    assert out.blocks == []
    assert len(out.needs) == 1 and out.needs[0].startswith("ppt (NotOleFileError")
    assert out.needs[0].endswith(f"{LIBREOFFICE} to retry)")


def test_ppt_without_olefile_or_soffice_reports_the_extra(tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "olefile", None)
    _no_renderers(monkeypatch)
    row = _rows(tmp_path, monkeypatch, capsys, PPT)[str(PPT)]
    assert row["needs"] == ["olefile"]
    assert row["hint"] == "meltify doctor --install office"


def test_dib_pictures_get_a_bitmap_file_header():
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 50), "white").save(buf, "BMP")
    dib = buf.getvalue()[14:]
    assert blips.bmp(dib) == buf.getvalue()


def test_store_entries_sharing_one_picture_unpack_it_once(tmp_path, monkeypatch):
    pytest.importorskip("olefile")
    _soffice(monkeypatch, lambda: None)
    picture = card("LOGO 42", (300, 100))
    path = _deck_file(
        tmp_path / "d.ppt", [_slide(_chars("x"))], pictures=[_png_blip(picture)], repeat=200
    )
    unpacked = []
    real = blips._blip

    def blip(data, at, budget):
        unpacked.append(at)
        return real(data, at, budget)

    monkeypatch.setattr(blips, "_blip", blip)
    out = _convert(path, Src("d.ppt"))
    assert len(unpacked) == 1
    assert len(out.jobs) == 200 and len({id(j.data) for j in out.jobs}) == 1


def test_pictures_past_the_deck_budget_are_needs(tmp_path, monkeypatch):
    pytest.importorskip("olefile")
    _soffice(monkeypatch, lambda: None)
    pictures = [card(f"CHART {i}", (300, 100)) for i in range(3)]
    path = _deck_file(
        tmp_path / "d.ppt", [_slide(_chars("x"))], pictures=[_png_blip(p) for p in pictures]
    )
    # Room for the first two pictures only
    room = len(pictures[0]) + len(pictures[1])
    monkeypatch.setattr("meltify.converters.embeds.MAX_PICTURE_BYTES", room)
    out = _convert(path, Src("d.ppt"))
    assert [j.data for j in out.jobs] == pictures[:2]
    assert "1 image not read (over the 256 MiB picture total)" in out.needs
