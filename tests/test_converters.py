import io
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from meltify import converters
from meltify.cli import main
from meltify.commands.read import job_needs, member_name
from meltify.converters import Child, Converted, Entry, RecognizeJob, pick, suffix_of
from meltify.evidence import Src

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
    ".ipynb": "notebook",
    ".mbox": "mbox",
    ".db": "sqlite",
    ".sqlite": "sqlite",
    ".sqlite3": "sqlite",
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


@pytest.mark.parametrize("suffix", [".7z", ".hwp", ".tar.gz", ".svg"])
def test_missing_module_reports_needs_instead_of_crashing(tmp_path, monkeypatch, suffix):
    monkeypatch.setitem(
        converters.SUFFIXES, suffix, Entry("x", "meltify.converters.not_landed_yet:convert")
    )
    path = tmp_path / f"a{suffix}"
    path.write_bytes(b"\x00")
    kind, convert = pick(path)
    assert kind == "x"
    assert convert(path, Src(str(path))).needs == [
        f"unsupported {suffix} (meltify 0.2 converter missing)"
    ]


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
    over = {e.kind: e.over for e in converters.SNIFFS if isinstance(e.sniff, str)}
    assert over == {"kakao": frozenset({".txt", ".csv"}), "slack": frozenset({".zip"})}
    (tmp_path / "chat.txt").write_text("2026. 10. 5. 오전 9:00, A : hi\n")
    assert pick(tmp_path / "chat.txt")[0] in {"kakao", "text"}


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

    from meltify.commands.read import Reader

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
    # A scanner's page image with the OCR text layer it wrote on top
    scanned = doc.new_page()
    scanned.insert_image(scanned.rect, stream=card("SCANNED PAGE 98,765", (850, 1100)))
    scanned.insert_text((72, 72), "SCANNED PAGE 98,765 from the text layer", fontsize=12)
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
