import importlib
import io
import json
import shutil
import subprocess
import sys
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from meltify import converters
from meltify.cli import main
from meltify.converters import Child, Converted, Entry, RecognizeJob, pick, suffix_of
from meltify.converters.run import RunContext
from meltify.evidence import Src
from meltify.files import member_name
from meltify.melt import job_needs

PLANNED = {
    ".zip": "archive",
    ".tar": "archive",
    ".tgz": "archive",
    ".tar.gz": "archive",
    ".tbz2": "archive",
    ".tar.bz2": "archive",
    ".txz": "archive",
    ".tar.xz": "archive",
    ".7z": "archive",
    ".rar": "archive",
    ".hwp": "hwp",
    ".hwpx": "hwp",
    ".doc": "legacy",
    ".xls": "legacy",
    ".ppt": "legacy",
    ".odt": "odf",
    ".ods": "odf",
    ".odp": "odf",
    ".rtf": "rtf",
    ".pages": "iwork",
    ".numbers": "iwork",
    ".key": "iwork",
    ".svg": "svg",
    **dict.fromkeys((".emf", ".wmf", ".emz", ".wmz"), "metafile"),
    **dict.fromkeys((".wpd", ".wp", ".wp5", ".wp6"), "wordperfect"),
    ".ipynb": "notebook",
    ".mbox": "mbox",
    ".db": "sqlite",
    ".sqlite": "sqlite",
    ".sqlite3": "sqlite",
    ".gz": "archive",
    ".bz2": "archive",
    ".xz": "archive",
    ".dot": "legacy",
    ".xlt": "legacy",
    ".xlsb": "legacy",
    ".pps": "legacy",
    ".pot": "legacy",
    ".odg": "odf",
    ".ott": "odf",
    ".ots": "odf",
    ".otp": "odf",
    ".otg": "odf",
    ".parquet": "parquet",
    ".webarchive": "webarchive",
    ".epub": "epub",
    ".msg": "mail",
    ".html": "web",
    ".htm": "web",
    ".xhtml": "web",
    ".xps": "pdf",
    ".oxps": "pdf",
    ".fb2": "pdf",
    ".cbz": "pdf",
    ".mobi": "pdf",
    ".docm": "office",
    ".dotx": "office",
    ".dotm": "office",
    ".pptm": "office",
    ".potx": "office",
    ".potm": "office",
    ".ppsx": "office",
    ".ppsm": "office",
    ".show": "office",
    ".xltx": "sheet",
    ".xltm": "sheet",
    ".cell": "sheet",
    ".jp2": "image",
    ".j2k": "image",
    ".psd": "image",
    ".ico": "image",
    ".tga": "image",
    **dict.fromkeys((".opus", ".wma", ".aiff", ".aif", ".amr"), "media"),
    **dict.fromkeys((".m4v", ".wmv", ".3gp", ".mpg", ".mpeg", ".flv"), "media"),
}


def fake_zip(path: Path, src: Src) -> Converted:
    """Stand-in for the archive converter that hands every member back as a child"""
    out = Converted("archive")
    with zipfile.ZipFile(path) as z:
        out.children = [Child(n, src, z.read(n)) for n in z.namelist() if not n.endswith("/")]
    return out


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


@pytest.fixture
def archives(monkeypatch):
    monkeypatch.setitem(
        converters.SUFFIXES, ".zip", Entry("archive", "tests.test_converters:fake_zip")
    )


@pytest.mark.parametrize("suffix,kind", sorted(PLANNED.items()))
def test_planned_suffixes_are_registered(tmp_path, suffix, kind):
    path = tmp_path / f"a{suffix}"
    path.write_bytes(b"\x00not real")
    assert pick(path)[0] == kind


def test_every_registered_target_imports():
    entries = [*converters.SUFFIXES.values(), *converters.SNIFFS, converters.UNKNOWN]
    targets = {e.target for e in entries} | set(converters.SEALS)
    targets |= {e.sniff for e in converters.SNIFFS if isinstance(e.sniff, str)}
    for target in targets:
        module, _, name = target.partition(":")
        assert callable(getattr(importlib.import_module(module), name)), target


def test_broken_dependency_inside_a_module_still_raises(tmp_path, monkeypatch):
    (tmp_path / "broken_converter.py").write_text("import surely_missing_dep_xyz\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setitem(converters.SUFFIXES, ".brk", Entry("x", "broken_converter:convert"))
    with pytest.raises(ModuleNotFoundError):
        pick(tmp_path / "a.brk")
    sys.modules.pop("broken_converter", None)


def test_existing_formats_keep_their_kinds(tmp_path):
    for name, kind in [
        ("a.pdf", "pdf"),
        ("a.xlsx", "sheet"),
        ("a.eml", "mail"),
        ("a.txt", "text"),
        ("a.docx", "office"),
        ("a.heic", "image"),
        ("a.avif", "image"),
        ("a.mp3", "media"),
        ("a.mov", "media"),
        ("a.bin", "unknown"),
        ("noext", "unknown"),
    ]:
        (tmp_path / name).write_bytes(b"\x00plain")
        assert pick(tmp_path / name)[0] == kind, name


def test_double_suffix_wins_over_single(tmp_path):
    assert suffix_of(Path("x/report.v2.TAR.GZ")) == ".tar.gz"
    assert suffix_of(Path("my.notes.txt")) == ".txt"
    assert suffix_of(Path("noext")) == ""


def test_magic_bytes_pick_a_converter_for_unknown_names(tmp_path):
    for name, head, kind in [
        ("a.bin", b"%PDF-1.7\n", "pdf"),
        ("b", b"SQLite format 3\x00", "sqlite"),
        ("c.dat", b"{\\rtf1 hi}", "rtf"),
        ("d", b"PK\x03\x04rest", "archive"),
        ("e.img", b"\x89PNG\r\n\x1a\nrest", "image"),
        ("f.dat", b"PAR1rest", "parquet"),
        ("g", b"II*\x00rest", "image"),
        ("h", b"8BPSrest", "image"),
        ("i", b"\x00\x00\x00\x0cjP  \r\n\x87\nrest", "image"),
        ("j", b"Rar!\x1a\x07\x01\x00rest", "archive"),
        ("k.doc1", b"\xffWPC\x10\x00\x00\x00\x01\x0a\x02\x01rest", "wordperfect"),
        # WPG graphics share the header but not the document file type
        ("k.wpg", b"\xffWPC\x10\x00\x00\x00\x01\x16\x01\x00rest", "unknown"),
        ("k2", b"\xffWPC\x10\x00\x00\x00\x01\x16\x01\x00rest", "unknown"),
        ("l", b"\xd7\xcd\xc6\x9arest", "metafile"),
    ]:
        (tmp_path / name).write_bytes(head)
        assert pick(tmp_path / name)[0] == kind, name
    # A known suffix never falls back to magic bytes
    (tmp_path / "f.txt").write_bytes(b"%PDF-1.7\n")
    assert pick(tmp_path / "f.txt")[0] == "text"


def test_content_sniff_overrides_listed_suffixes_only(tmp_path, monkeypatch):
    monkeypatch.setattr(
        converters,
        "SNIFFS",
        [
            Entry(
                "kakao",
                "meltify.converters.text:convert",
                lambda p, head: head.startswith(b"KakaoTalk"),
                frozenset({".txt"}),
            )
        ],
    )
    (tmp_path / "chat.txt").write_bytes(b"KakaoTalk Chats with A")
    (tmp_path / "chat.md").write_bytes(b"KakaoTalk Chats with A")
    (tmp_path / "plain.txt").write_bytes(b"hello")
    assert pick(tmp_path / "chat.txt")[0] == "kakao"
    assert pick(tmp_path / "chat.md")[0] == "text"
    assert pick(tmp_path / "plain.txt")[0] == "text"


def test_chat_sniffs_are_wired_but_skip_until_their_modules_land(tmp_path):
    over = {e.kind: e.over for e in converters.SNIFFS if e.kind in ("kakao", "slack")}
    assert over == {"kakao": frozenset({".txt", ".csv"}), "slack": frozenset({".zip"})}
    (tmp_path / "chat.txt").write_text("2026. 10. 5. 오전 9:00, A : hi\n")
    assert pick(tmp_path / "chat.txt")[0] in {"kakao", "text"}


def _stored_first(path: Path, mimetype: str) -> None:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", mimetype, compress_type=zipfile.ZIP_STORED)
        z.writestr("content.xml", "<x/>", compress_type=zipfile.ZIP_DEFLATED)


def test_document_zips_under_unknown_names_are_not_unpacked_as_archives(tmp_path):
    from tests.fixtures import make_docs

    for name, mimetype, kind in [
        ("a.bin", "application/vnd.oasis.opendocument.text", "odf"),
        ("b", "application/vnd.oasis.opendocument.graphics-template", "odf"),
        ("c.dat", "application/epub+zip", "epub"),
        ("d", "application/hwp+zip", "hwp"),
        # A formula document is no text odf reads, so it stays an archive
        ("e", "application/vnd.oasis.opendocument.formula", "archive"),
    ]:
        _stored_first(tmp_path / name, mimetype)
        assert pick(tmp_path / name)[0] == kind, name
    picture = make_docs.card("hi")
    docx = make_docs.embedded_docx(tmp_path / "memo.docx", picture)
    xlsx = make_docs.embedded_xlsx(tmp_path / "book.xlsx", picture)
    assert pick(docx.rename(tmp_path / "memo.download"))[0] == "office"
    assert pick(xlsx.rename(tmp_path / "book"))[0] == "sheet"
    plain = tmp_path / "plain.bin"
    plain.write_bytes(_zip({"a.txt": b"hi"}))
    assert pick(plain)[0] == "archive"


def test_recognize_job_and_child_need_exactly_one_payload(tmp_path):
    src = Src("a.pdf", page=2)
    with pytest.raises(ValueError):
        RecognizeJob("image", src)
    with pytest.raises(ValueError):
        RecognizeJob("image", src, path=tmp_path, data=b"x")
    with pytest.raises(ValueError):
        RecognizeJob("page", Src("a.pdf"), path=tmp_path)
    job = RecognizeJob("image", Src("a.pdf", page=3, img=1), data=b"png", rect=(0, 0, 10, 10))
    assert job.src.cite() == "a.pdf#p3#img1"
    with pytest.raises(ValueError):
        Child("a.txt", src)
    assert Child("a.txt", src, b"hi").data == b"hi"


def test_job_needs_match_0_1_wording(tmp_path):
    p = tmp_path / "a.pdf"
    assert job_needs([RecognizeJob("image", Src("a.png"), path=p)]) == ["ocr"]
    assert job_needs([RecognizeJob("audio", Src("a.mp3"), path=p)]) == ["media"]
    pages = [RecognizeJob("page", Src("a.pdf", page=n), path=p) for n in (2, 5)]
    assert job_needs(pages) == ["ocr pages 2,5"]
    two = [RecognizeJob("image", Src("a.docx", img=n), data=b"x") for n in (1, 2)]
    assert job_needs(two) == ["ocr 2 images"]
    assert job_needs([]) == []


@pytest.mark.parametrize(
    "raw,want",
    [
        ("docs/b.pdf", "docs/b.pdf"),
        ("../../etc/passwd", "etc/passwd"),
        ("/abs/x.txt", "abs/x.txt"),
        ("C:\\Users\\me\\x.txt", "Users/me/x.txt"),
        ("a/./b//c.txt", "a/b/c.txt"),
        ("..", "attachment-3"),
        (None, "attachment-3"),
    ],
)
def test_member_name_keeps_directories_but_never_escapes(raw, want):
    assert member_name(raw, 3) == want


def _read(tmp_path, monkeypatch, capsys, *paths):
    monkeypatch.chdir(tmp_path)
    code = main(["read", *map(str, paths), "--json", "--limit", "0"])
    return code, json.loads(capsys.readouterr().out)


def test_archive_children_cite_each_level_with_att(tmp_path, monkeypatch, capsys, archives):
    from tests.fixtures.make_docs import calendar_xlsx

    inner = _zip({"c.txt": b"inner line\n"})
    outer = _zip(
        {
            "docs/b.txt": b"hello from b\n",
            "docs__b.txt": b"flat twin\n",
            "x.zip": inner,
            "cal.xlsx": calendar_xlsx(),
            "../escape.txt": b"nope\n",
        }
    )
    (tmp_path / "a.zip").write_bytes(outer)
    code, out = _read(tmp_path, monkeypatch, capsys, Path("a.zip"))
    assert code == 0
    by_cite = {r["cite"]: r for r in out["results"]}
    assert set(by_cite) == {
        "a.zip",
        "a.zip#att=docs/b.txt",
        "a.zip#att=docs__b.txt",
        "a.zip#att=x.zip",
        "a.zip#att=x.zip#att=c.txt",
        "a.zip#att=cal.xlsx",
        "a.zip#att=escape.txt",
    }
    nested = by_cite["a.zip#att=x.zip#att=c.txt"]
    assert nested["src"] == {"path": "a.zip", "parts": ["x.zip", "c.txt"]}
    assert "## a.zip#att=x.zip#att=c.txt:1\n1| inner line" in Path(nested["out"]).read_text()
    sheet = Path(by_cite["a.zip#att=cal.xlsx"]["out"]).read_text()
    assert "## a.zip#att=cal.xlsx#Calendar" in sheet
    outs = [Path(r["out"]).resolve() for r in out["results"]]
    assert len(set(outs)) == len(outs)
    assert "flat twin" in Path(by_cite["a.zip#att=docs__b.txt"]["out"]).read_text()
    assert "hello from b" in Path(by_cite["a.zip#att=docs/b.txt"]["out"]).read_text()
    out_dir = (tmp_path / "meltify-out").resolve()
    saved = [p for p in out_dir.rglob("*") if p.is_file()]
    assert all(p.is_relative_to(out_dir) for p in saved)
    assert not (tmp_path / "escape.txt").exists()


def test_depth_limit_is_shared_by_archives_and_mail(tmp_path, monkeypatch, capsys, archives):
    from tests.fixtures.make_docs import nested_mail

    mail = nested_mail(tmp_path / "fwd.eml", levels=1).read_bytes()
    data = _zip({"m.eml": mail})
    data = _zip({"inner.zip": data})
    (tmp_path / "deep.zip").write_bytes(data)
    _, out = _read(tmp_path, monkeypatch, capsys, Path("deep.zip"))
    cites = sorted(r["cite"] for r in out["results"])
    assert cites == [
        "deep.zip",
        "deep.zip#att=inner.zip",
        "deep.zip#att=inner.zip#att=m.eml",
        "deep.zip#att=inner.zip#att=m.eml#att=attachment-1.eml",
    ]
    last = next(r for r in out["results"] if r["cite"] == cites[-1])
    assert last["needs"] == ["1 nested items beyond depth 3"]


def test_jobs_are_queued_on_the_reader(tmp_path):
    from PIL import Image

    from meltify.melt import Reader

    Image.new("RGB", (8, 8)).save(tmp_path / "a.png")
    (tmp_path / "b.mp3").write_bytes(b"\x00")
    reader = Reader(tmp_path / "out", {tmp_path / "a.png": "a.png", tmp_path / "b.mp3": "b.mp3"})
    rows = reader.one(tmp_path / "a.png", Src("a.png")) + reader.one(
        tmp_path / "b.mp3", Src("b.mp3")
    )
    jobs = [j for o in reader.outputs for j in o.converted.jobs]
    assert [(j.kind, j.src.cite()) for j in jobs] == [("image", "a.png"), ("audio", "b.mp3")]
    # Nothing is written or marked pending until the recognition stage settles
    assert [r["needs"] for r in rows] == [[], []]
    assert not (tmp_path / "out" / "a.png.md").exists()
    assert reader.finish(None) == []
    assert [r["needs"] for r in rows] == [["ocr"], ["media"]]
    assert "<!-- meltify needs: ocr -->" in (tmp_path / "out" / "a.png.md").read_text()
    assert "<!-- meltify needs: media -->" in (tmp_path / "out" / "b.mp3.md").read_text()


def _heic(path: Path) -> Path:
    from PIL import Image

    png = path.with_suffix(".png")
    Image.new("RGB", (64, 32), "red").save(png)
    try:
        import pi_heif

        pi_heif.from_pillow(Image.open(png)).save(path)
        return path
    except Exception:  # noqa: BLE001
        pass
    if shutil.which("sips"):
        subprocess.run(
            ["sips", "-s", "format", "heic", str(png), "--out", str(path)],
            check=True,
            capture_output=True,
        )
        return path
    pytest.skip("no HEIC encoder here: pi-heif ships decode only and macOS sips is missing")


def test_heic_opens_in_pillow_once_imaging_is_imported(tmp_path):
    from PIL import Image

    from meltify import imaging  # noqa: F401

    path = _heic(tmp_path / "photo.heic")
    with Image.open(path) as im:
        assert im.size == (64, 32)
        r, g, b = im.convert("RGB").getpixel((5, 5))
    assert r > 200 and g < 50 and b < 50


def test_searchable_scans_are_not_read_twice(tmp_path):
    import pymupdf

    from meltify.converters.pdf import convert
    from tests.fixtures.make_docs import card

    doc = pymupdf.open()
    # A scanner's page image with the invisible OCR text layer it wrote on top
    scanned = doc.new_page()
    scanned.insert_image(scanned.rect, stream=card("SCANNED PAGE 98,765", (850, 1100)))
    scanned.insert_text(
        (72, 72), "SCANNED PAGE 98,765 from the text layer", fontsize=12, render_mode=3
    )
    # A large figure that still leaves room for the page's own text
    figure = doc.new_page()
    figure.insert_text((72, 40), "Figure 1 shows the quarterly numbers", fontsize=12)
    r = figure.rect
    figure.insert_image(
        pymupdf.Rect(0, 60, r.width, 60 + r.height * 0.7), stream=card("FIG 1", (850, 700))
    )
    doc.save(tmp_path / "scan.pdf")

    out = convert(tmp_path / "scan.pdf", Src("scan.pdf"))
    assert [b.src.cite() for b in out.blocks] == ["scan.pdf#p1", "scan.pdf#p2"]
    assert [j.src.cite() for j in out.jobs] == ["scan.pdf#p2#img1"]


def test_control_heavy_bytes_go_to_the_fallback_not_text(tmp_path, monkeypatch):
    from meltify.converters import text

    monkeypatch.setattr("meltify.converters.run.current", lambda: RunContext(shallow=True))
    junk = tmp_path / "weird.bin2"
    junk.write_bytes(bytes(range(1, 256)) * 40)
    out = text.convert_unknown(junk, Src("weird.bin2"))
    assert out.kind == "unknown" and out.blocks == [] and out.needs == ["unsupported format"]
    # Tabs, form feeds and ANSI colors are still text
    log = tmp_path / "build.out"
    log.write_bytes(b"\x1b[32mok\x1b[0m\tdone\x0c\n" * 50)
    assert text.convert_unknown(log, Src("build.out")).kind == "text"


def _texts(blocks):
    return [b.text for b in blocks]


def test_odt_pictures_sit_after_their_paragraph_ahead_of_the_tables():
    # odf cites paragraphs by line and appends tables by name after all of them
    src = Src("a.odt")
    blocks = [
        converters.Block(replace(src, line=1), "lines 1-50"),
        converters.Block(replace(src, line=51), "lines 51-60"),
        converters.Block(replace(src, sheet="Table1"), "table"),
    ]
    extra = [
        converters.Block(replace(src, para=3, img=1), "picture in para 3"),
        converters.Block(replace(src, para=55, img=2), "picture in para 55"),
    ]
    assert _texts(converters.place(blocks, extra)) == [
        "lines 1-50",
        "picture in para 3",
        "lines 51-60",
        "picture in para 55",
        "table",
    ]


def test_pages_pictures_sit_after_their_paragraph_around_anchored_tables():
    # Pages cites a table by name and by the line of the paragraph it is anchored in
    src = Src("a.pages")
    blocks = [
        converters.Block(replace(src, line=1), "lines 1-5"),
        converters.Block(replace(src, sheet="Costs", line=6), "table"),
        converters.Block(replace(src, line=7), "lines 7-12"),
    ]
    extra = [
        converters.Block(replace(src, para=2, img=1), "picture in para 2"),
        converters.Block(replace(src, para=10, img=2), "picture in para 10"),
    ]
    assert _texts(converters.place(blocks, extra)) == [
        "lines 1-5",
        "picture in para 2",
        "table",
        "lines 7-12",
        "picture in para 10",
    ]


def test_spreadsheet_extras_still_follow_their_sheet():
    src = Src("a.ods")
    blocks = [
        converters.Block(replace(src, sheet="One"), "one"),
        converters.Block(replace(src, sheet="Two"), "two"),
    ]
    extra = [
        converters.Block(replace(src, sheet="One", img=1), "chart on one"),
        converters.Block(replace(src, img=2), "picture citing no sheet"),
    ]
    assert _texts(converters.place(blocks, extra)) == [
        "one",
        "chart on one",
        "two",
        "picture citing no sheet",
    ]


def _peak(fn, *args):
    import tracemalloc

    tracemalloc.start()
    try:
        fn(*args)
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_an_unknown_binary_is_sniffed_without_reading_all_of_it(tmp_path, monkeypatch):
    from meltify.converters import text

    monkeypatch.setattr("meltify.converters.run.current", lambda: RunContext(shallow=True))
    blob = tmp_path / "blob.xyz"
    blob.write_bytes(b"\x00\x01" * (8 << 20))
    out = text.convert_unknown(blob, Src("blob.xyz"))
    assert out.needs == ["unsupported format"]
    assert _peak(text.convert_unknown, blob, Src("blob.xyz")) < 1 << 20


def test_edi_and_marc_separators_are_text(tmp_path):
    from meltify.converters import text

    # MARC ends fields with 0x1E and records with 0x1D, and splits subfields with 0x1F
    marc = b"00714cam  2200205 a 4500\x1e\x1fa0123456789\x1fbHarry Potter\x1e\x1d" * 40
    assert not text.binary(marc[:4096])
    path = tmp_path / "records.mrc"
    path.write_bytes(marc)
    assert text.convert_unknown(path, Src("records.mrc")).kind == "text"


def test_a_big_text_file_streams_into_blocks(tmp_path):
    from meltify.converters import text

    path = tmp_path / "big.csv"
    row = "12345,alpha beta gamma delta epsilon zeta eta theta,0.6135968270065508\n"
    path.write_text(row * 100_000)
    size = path.stat().st_size
    out = text.convert(path, Src("big.csv"))
    assert out.blocks[-1].src.line == 99_801 and out.blocks[0].text.startswith("     1| 12345")
    # The blocks themselves are about one copy of the file
    assert _peak(text.convert, path, Src("big.csv")) < size * 2.5


@pytest.mark.parametrize("seed", range(40))
def test_streamed_blocks_match_the_whole_file_decode(tmp_path, monkeypatch, seed):
    import random

    from meltify.converters import text

    rng = random.Random(seed)
    pieces = ["a", "b c", "한글", "\r\n", "\n", "\r", "\x0b", "\x0c", "\x1c", "\x1e", " "]
    pieces += ["\x85", "", " ", "\t", "﻿", "x" * 50]
    body = "".join(rng.choice(pieces) for _ in range(rng.randint(0, 3000)))
    encoding = rng.choice(["utf-8", "utf-8-sig", "cp949", "utf-16", "broken"])
    if encoding == "broken":
        data = body.encode("utf-8") + b"\xff\xfe\x80 tail\n"
    else:
        data = body.encode(encoding, errors="replace")
    path = tmp_path / "a.txt"
    path.write_bytes(data)
    # Tiny chunks, so CRLF pairs and multibyte characters straddle chunk edges
    monkeypatch.setattr(text, "CHUNK", rng.choice([1, 2, 3, 7, 64]))
    want = text.numbered(text.decode(data), Src("a.txt"))
    assert text.numbered_file(path, Src("a.txt")) == want


def _place_before(blocks, extra):
    """place as it was, comparing every key on every insert, kept as the reference"""
    import math

    sheets = list(dict.fromkeys(b.src.sheet for b in blocks if b.src.sheet))
    paged = any(b.src.page for b in blocks)
    slides = any(b.src.slide for b in blocks)

    def order(src, extra):
        unnamed = math.inf if extra else 0
        flow = src.para if extra else src.line or src.para
        if flow:
            sheet = 0
        elif src.sheet:
            sheet = sheets.index(src.sheet) + 1 if src.sheet in sheets else len(sheets) + 1
        else:
            sheet = unnamed if sheets else 0
        para = math.inf if src.para is None else src.para
        return (
            src.page or (unnamed if paged else 0),
            src.slide or (unnamed if slides else 0),
            sheet,
            para if extra else src.line or src.para or 0,
        )

    out = list(blocks)
    for b in extra:
        at = order(b.src, True)
        out.insert(next((i for i, x in enumerate(out) if order(x.src, False) > at), len(out)), b)
    return out


@pytest.mark.parametrize("seed", range(300))
def test_place_matches_the_reference_order(seed):
    import random

    rng = random.Random(seed)

    def pick(*values):
        return rng.choice([None, *values])

    def src(n):
        return Src(
            "a",
            page=pick(1, 2, 3),
            slide=pick(1, 2),
            sheet=pick("S1", "S2", "S3"),
            line=pick(1, 5, 9, 40),
            para=pick(1, 3, 7, 20),
            img=n,
        )

    blocks = [converters.Block(src(None), f"b{i}") for i in range(rng.randint(0, 25))]
    extra = [converters.Block(src(i), f"e{i}") for i in range(rng.randint(0, 25))]
    assert _texts(converters.place(blocks, extra)) == _texts(_place_before(blocks, extra))


def test_place_stays_fast_on_big_documents():
    import time

    src = Src("a.docx")
    blocks = [converters.Block(replace(src, line=n * 10), "text") for n in range(1, 20_001)]
    extra = [converters.Block(replace(src, para=n * 7, img=n), "pic") for n in range(1, 5_001)]
    started = time.perf_counter()
    out = converters.place(blocks, extra)
    assert time.perf_counter() - started < 2
    assert [b.src.img for b in out if b.src.img][:3] == [1, 2, 3]
