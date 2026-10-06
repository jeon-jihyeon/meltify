import gzip
import hashlib
import importlib.util
import json
import os
import shutil
import struct
import zipfile
from pathlib import Path

import pytest

from meltify import passwords
from meltify.cli import main
from meltify.converters import iwa, iwork
from meltify.converters.iwork import convert
from meltify.evidence import Src
from meltify.passwords import NEEDS_CRYPTO

SAMPLES = Path(__file__).parent / "fixtures" / "samples" / "docs"
JPEG = b"\xff\xd8\xff\xe0 fake preview"
PNG = b"\x89PNG\r\n\x1a\n fake picture"


@pytest.fixture(autouse=True)
def no_password(monkeypatch):
    monkeypatch.delenv("MELTIFY_PASSWORD", raising=False)


def _melt(path: Path):
    return convert(path, Src(path.name))


def _shut(path: Path) -> str:
    """The needs line of a file that stays shut"""
    with pytest.raises(passwords.Locked) as e:
        _melt(path)
    return str(e.value)


# A tiny protobuf and IWA writer, so tests can build the object graphs Apple writes


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        out.append((n & 0x7F) | (0x80 if n > 0x7F else 0))
        n >>= 7
        if not n:
            return bytes(out)


def _msg(*fields: tuple[int, int | bytes | str]) -> bytes:
    out = b""
    for n, v in fields:
        if isinstance(v, int):
            out += _varint(n << 3) + _varint(v)
        else:
            v = v.encode() if isinstance(v, str) else v
            out += _varint(n << 3 | 2) + _varint(len(v)) + v
    return out


def _ref(oid: int) -> bytes:
    return _msg((1, oid))


def _iwa(objs: dict[int, tuple[int, bytes]]) -> bytes:
    stream = b""
    for oid, (kind, payload) in objs.items():
        info = _msg((1, oid), (2, _msg((1, kind), (3, len(payload)))))
        stream += _varint(len(info)) + info + payload
    # One literal-only Snappy block per chunk keeps the writer trivial
    out = b""
    for start in range(0, len(stream), 60000):
        part = stream[start : start + 60000]
        n = len(part) - 1
        block = _varint(len(part)) + bytes([62 << 2]) + n.to_bytes(3, "little") + part
        out += b"\x00" + len(block).to_bytes(3, "little") + block
    return out


def _cell(key: int) -> bytes:
    # A version 5 text cell: version, type 3, flags with the string bit, then the key
    return bytes([5, 3, 0, 0]) + b"\x00" * 4 + struct.pack("<I", 8) + struct.pack("<I", key)


def _table(base: int, name: str, rows: list[list[str]]) -> dict[int, tuple[int, bytes]]:
    strings = {s: i + 1 for i, s in enumerate(dict.fromkeys(s for r in rows for s in r if s))}
    row_msgs = []
    for r, row in enumerate(rows):
        buf, offsets = b"", b""
        for value in row:
            if value:
                offsets += struct.pack("<h", len(buf))
                buf += _cell(strings[value])
            else:
                offsets += struct.pack("<h", -1)
        row_msgs.append((5, _msg((1, r), (3, buf), (4, offsets))))
    store = _msg(
        (3, _msg((1, _msg((1, 0), (2, _ref(base + 2)))))),
        (4, _ref(base + 3)),
    )
    entries = [(3, _msg((1, k), (3, s))) for s, k in strings.items()]
    return {
        base: (6000, _msg((2, _ref(base + 1)))),
        base + 1: (6001, _msg((4, store), (6, len(rows)), (7, len(rows[0])), (8, name))),
        base + 2: (6002, _msg(*row_msgs)),
        base + 3: (6005, _msg(*entries)),
    }


def _pages_objects() -> dict[int, tuple[int, bytes]]:
    body = "Title\n\nSee the table \ufffc\nA picture \ufffc\nEnd 😀"
    anchor_at = len("Title\n\nSee the table ".encode("utf-16-le")) // 2
    picture_at = len("Title\n\nSee the table \ufffc\nA picture ".encode("utf-16-le")) // 2
    attachments = _msg(
        (1, _msg((1, anchor_at), (2, _ref(30)))),
        (1, _msg((1, picture_at), (2, _ref(31)))),
    )
    objs = {
        10: (10000, _msg((4, _ref(20)), (20, _ref(45)))),
        20: (2001, _msg((3, body), (9, attachments))),
        30: (2003, _msg((1, _ref(100)))),
        31: (2003, _msg((1, _ref(50)))),
        40: (2011, _msg((2, _ref(41)))),
        41: (2001, _msg((3, "Floating box"))),
        # The document's own list of drawables that float outside the text
        45: (10015, _msg((1, _ref(40)))),
        50: (3005, _msg((15, _ref(7)))),
        60: (11006, _msg((4, _msg((1, 7), (3, "pic.png"))))),
    }
    objs.update(_table(100, "Prices", [["Item", "Cost"], ["Tea", ""]]))
    return objs


def _bundle(path: Path, objs, extra: dict[str, bytes] | None = None) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Index/Document.iwa", _iwa(objs))
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return path


def test_numbers_cells_cite_sheet_and_table():
    pytest.importorskip("numbers_parser")
    out = _melt(SAMPLES / "numbers-parser-issue-18.numbers")
    (block,) = out.blocks
    assert block.src.cite() == "numbers-parser-issue-18.numbers#Sheet 1>Table 1"
    lines = block.text.splitlines()
    assert lines[0] == "| row | A | B | C | D | E |"
    # B3:D3 is merged, so C3 and D3 stay blank instead of repeating it
    assert lines[4] == "| 3 | A3 | B3:D3 |  |  |  |"
    assert not out.jobs and not out.needs


def test_encrypted_numbers_needs_a_password():
    assert _shut(SAMPLES / "numbers-parser-encrypted.numbers") == passwords.LOCKED


def test_encrypted_numbers_opens_with_the_password(monkeypatch):
    pytest.importorskip("numbers_parser")
    pytest.importorskip("cryptography")
    monkeypatch.setenv("MELTIFY_PASSWORD", "s3cr3t")
    out = _melt(SAMPLES / "numbers-parser-encrypted.numbers")
    (block,) = out.blocks
    assert block.text.splitlines()[2] == "| 1 | Decryption | Algorithm | Stream |"
    monkeypatch.setenv("MELTIFY_PASSWORD", "s3cret")
    assert _shut(SAMPLES / "numbers-parser-encrypted.numbers") == passwords.WRONG


def test_encrypted_iwork_without_cryptography_names_the_extra(monkeypatch):
    real = importlib.util.find_spec

    def find_spec(name, *args):
        return None if name == "cryptography" else real(name, *args)

    monkeypatch.setattr(iwork.importlib.util, "find_spec", find_spec)
    monkeypatch.setenv("MELTIFY_PASSWORD", "s3cr3t")
    assert _shut(SAMPLES / "numbers-parser-encrypted.numbers") == NEEDS_CRYPTO


def test_numbers_without_extra_falls_back_to_preview(monkeypatch):
    real = importlib.util.find_spec

    def find_spec(name, *args):
        return None if name == "numbers_parser" else real(name, *args)

    monkeypatch.setattr(iwork.importlib.util, "find_spec", find_spec)
    path = SAMPLES / "numbers-parser-issue-18.numbers"
    with zipfile.ZipFile(path) as z:
        has_preview = "preview.jpg" in z.namelist()
    out = _melt(path)
    assert out.needs[0] == "iwork extra (meltify doctor --install iwork)"
    assert out.needs[1:] == (["iwork preview only"] if has_preview else ["iwork preview missing"])


def test_keynote_slides_follow_the_show_and_unpack_a_nested_index():
    # Keynote 2018 flattens the package into a folder and zips Index/ once more
    out = _melt(SAMPLES / "docling-keynote-2018.key")
    assert [(b.src.cite(), b.text) for b in out.blocks] == [
        ("docling-keynote-2018.key#slide1", "Libreoffice 6.2\n19 February 2019"),
        (
            "docling-keynote-2018.key#slide2",
            "Test running...\nI save in Apple Key App as default .key\n"
            "Open in Libreoffice 6.2\nWindows 10 x86",
        ),
    ]
    assert not out.jobs and not out.needs


def test_keynote_with_an_empty_slide_falls_back_to_the_preview():
    out = _melt(SAMPLES / "keynote-parser-table.key")
    (job,) = out.jobs
    assert job.kind == "image"
    assert job.src.cite() == "keynote-parser-table.key#att=preview.jpg"
    assert job.data.startswith(b"\xff\xd8\xff")
    assert out.needs == ["iwork preview only"]


def test_keynote_reads_nested_slides_placeholders_and_notes(tmp_path):
    objs = {
        1: (1, _msg((2, _ref(2)))),
        2: (2, _msg((3, _msg((2, _ref(3)))))),
        # The second slide is tucked under the first in the navigator
        3: (4, _msg((2, _ref(10)), (1, _ref(4)))),
        4: (4, _msg((2, _ref(11)))),
        10: (5, _msg((5, _ref(20)), (7, _ref(21)), (27, _ref(30)))),
        11: (5, _msg((6, _ref(22)))),
        20: (7, _msg((1, _msg((2, _ref(40)))))),
        21: (2011, _msg((2, _ref(41)))),
        22: (7, _msg((1, _msg((2, _ref(42)))))),
        30: (15, _msg((1, _ref(43)))),
        40: (2001, _msg((3, "Hello"))),
        41: (2001, _msg((3, "A shape\n\nwith two lines"))),
        42: (2001, _msg((3, "Second"))),
        43: (2001, _msg((3, "Say hi"))),
    }
    out = _melt(_bundle(tmp_path / "deck.key", objs))
    assert [(b.src.cite(), b.text) for b in out.blocks] == [
        ("deck.key#slide1", "Hello\nA shape\nwith two lines\n\nNotes:\nSay hi"),
        ("deck.key#slide2", "Second"),
    ]


def test_pages_cites_paragraphs_tables_pictures_and_text_boxes(tmp_path):
    path = _bundle(tmp_path / "doc.pages", _pages_objects(), {"Data/pic.png": PNG})
    out = _melt(path)
    assert [(b.src.cite(), b.text) for b in out.blocks] == [
        ("doc.pages:1", "1| Title\n3| See the table"),
        (
            "doc.pages#Prices:3",
            "| row | A | B |\n|---|---|---|\n| 1 | Item | Cost |\n| 2 | Tea |  |",
        ),
        # Text outside the body flow continues the paragraph count after it
        ("doc.pages:4", "4| A picture\n5| End 😀\n6| Floating box"),
    ]
    (job,) = out.jobs
    assert job.src.cite() == "doc.pages#para4#img1" and job.data == PNG
    assert not out.needs


def test_pages_bundle_folder_reads_like_the_zip(tmp_path):
    zipped = _bundle(tmp_path / "doc.pages", _pages_objects(), {"Data/pic.png": PNG})
    folder = tmp_path / "folder" / "doc.pages"
    with zipfile.ZipFile(zipped) as z:
        z.extractall(folder)
    assert [b.text for b in _melt(folder).blocks] == [b.text for b in _melt(zipped).blocks]


def _encrypt_bundle(path: Path, source: Path, secret: str) -> Path:
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    salt, iv, rounds = os.urandom(16), os.urandom(16), 1000
    key = hashlib.pbkdf2_hmac("sha1", secret.encode(), salt, rounds, 16)

    def seal(data: bytes, iv: bytes, pad: bool = True) -> bytes:
        if pad:
            p = padding.PKCS7(128).padder()
            data = p.update(data) + p.finalize()
        e = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        return e.update(data) + e.finalize()

    check = os.urandom(32)
    verifier = struct.pack("<HHI", 2, 1, rounds) + salt + iv
    verifier += seal(check + hashlib.sha256(check).digest(), iv, pad=False)
    with zipfile.ZipFile(source) as src, zipfile.ZipFile(path, "w") as z:
        for name in src.namelist():
            nonce = os.urandom(16)
            z.writestr(name, nonce + seal(os.urandom(16) + src.read(name), nonce) + os.urandom(20))
        z.writestr(".iwpv2", verifier)
        z.writestr(".iwph", "hint")
    return path


def test_encrypted_pages_opens_with_the_password(tmp_path, monkeypatch):
    pytest.importorskip("cryptography")
    plain = _bundle(tmp_path / "plain.pages", _pages_objects(), {"Data/pic.png": PNG})
    locked = _encrypt_bundle(tmp_path / "locked.pages", plain, "open sesame")
    assert _shut(locked) == passwords.LOCKED
    monkeypatch.setenv("MELTIFY_PASSWORD", "open sesame")
    out = _melt(locked)
    assert [b.text for b in out.blocks] == [b.text for b in _melt(plain).blocks]
    assert out.jobs[0].data == PNG
    monkeypatch.setenv("MELTIFY_PASSWORD", "open sesam")
    assert _shut(locked) == passwords.WRONG


def test_iwork09_pages_reads_its_xml_without_template_text(tmp_path):
    sf = "http://developer.apple.com/namespaces/sf"
    sfa = "http://developer.apple.com/namespaces/sfa"
    xml = f"""<sl:document xmlns:sl="x" xmlns:sf="{sf}" xmlns:sfa="{sfa}">
      <sf:header><sf:p>Page header</sf:p></sf:header>
      <sf:text-storage><sf:text-body>
        <sf:p>First<sf:br/>line</sf:p>
        <sf:p><sf:ghost-text>Type here</sf:ghost-text></sf:p>
        <sf:tabular-model><sf:grid sf:numcols="2"><sf:datasource>
          <sf:t><sf:ct sfa:s="A"/></sf:t><sf:n sf:v="7"/>
        </sf:datasource></sf:grid></sf:tabular-model>
        <sf:p>Last</sf:p>
      </sf:text-body></sf:text-storage></sl:document>"""
    path = tmp_path / "old.pages"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("index.xml.gz", gzip.compress(xml.encode()))
    assert [(b.src.cite(), b.text) for b in _melt(path).blocks] == [
        ("old.pages:1", "1| First line"),
        ("old.pages:3", "| row | A | B |\n|---|---|---|\n| 1 | A | 7 |"),
        ("old.pages:4", "4| Last"),
    ]


def test_older_pages_uses_quicklook_preview(tmp_path):
    path = tmp_path / "a.pages"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Index/Document.iwa", b"")
        z.writestr("QuickLook/Thumbnail.jpg", b"\xff\xd8\xff small")
        z.writestr("QuickLook/Preview.jpg", JPEG)
    (job,) = _melt(path).jobs
    assert job.src.cite() == "a.pages#att=QuickLook/Preview.jpg"
    assert job.data == JPEG


def test_pages_without_preview_says_so(tmp_path):
    path = tmp_path / "a.pages"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Index/Document.iwa", b"")
    out = _melt(path)
    assert out.needs == ["iwork preview missing"] and not out.jobs


def test_snappy_copies_and_bad_streams():
    # A literal "ab" then a 1-byte-offset copy of 6 that overlaps itself
    block = _varint(8) + bytes([1 << 2]) + b"ab" + bytes([(2 << 2) | 1, 2])
    assert iwa.unsnappy(block, 100) == b"abababab"
    with pytest.raises(iwa.IwaError):
        iwa.unsnappy(_varint(9) + block[1:], 100)
    with pytest.raises(iwa.IwaError):
        iwa.unsnappy(block, 4)


def test_read_cites_numbers_end_to_end(tmp_path, monkeypatch, capsys):
    pytest.importorskip("numbers_parser")
    shutil.copy(SAMPLES / "numbers-parser-issue-18.numbers", tmp_path / "n.numbers")
    monkeypatch.chdir(tmp_path)
    main(["read", "n.numbers", "--json", "--limit", "0"])
    (row,) = json.loads(capsys.readouterr().out)["results"]
    assert row["kind"] == "iwork"
    assert "## n.numbers#Sheet 1>Table 1\n" in Path(row["out"]).read_text()


def test_snappy_runs_expand_in_one_step_and_files_share_one_cap(monkeypatch):
    import time

    # A literal then half a million copies of the byte before, the shape of a Snappy bomb
    copies = 1 << 19
    block = _varint(1 + copies * 64) + bytes([0]) + b"A" + bytes([(63 << 2) | 2, 1, 0]) * copies
    start = time.monotonic()
    assert iwa.unsnappy(block, 1 << 30) == b"A" * (1 + copies * 64)
    # A byte at a time took over a second here
    assert time.monotonic() - start < 0.5
    one = _iwa({1: (2001, _msg((3, "x" * 100)))})
    monkeypatch.setattr(iwa, "MAX_STREAM_BYTES", len(one) * 3 // 2)
    assert len(iwa.objects([one])) == 1
    with pytest.raises(iwa.IwaError, match="over the limit"):
        iwa.objects([one, one])


def test_index_files_share_one_cap_and_a_bloated_one_is_left_out(tmp_path, monkeypatch):
    good = _iwa(_pages_objects())
    path = tmp_path / "doc.pages"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Index/Document.iwa", good)
        # Zeros deflate far past 100:1, the way a zip bomb does
        z.writestr("Index/Bloat.iwa", bytes(2 << 20))
        z.writestr("Index/Again.iwa", good)
        z.writestr("Data/pic.png", PNG)
    monkeypatch.setattr(iwa, "MAX_STREAM_BYTES", len(good) * 3 // 2)
    out = _melt(path)
    assert out.blocks[0].text == "1| Title\n3| See the table"
    assert out.needs == [
        "1 iwork index file not read (expands over 100:1)",
        f"1 iwork index file not read (over the {len(good) * 3 // 2} bytes index total)",
    ]


def _anchored(paras: list[str], targets: list[int]) -> bytes:
    """A body storage whose paragraphs each end in an object replacement anchoring a target"""
    text, runs = "", []
    for para, target in zip(paras, targets, strict=True):
        text += f"{para} "
        runs.append((1, _msg((1, len(text.encode("utf-16-le")) // 2), (2, _ref(target)))))
        text += "￼\n"
    return _msg((3, text + "End"), (9, _msg(*runs)))


def test_attachments_that_loop_and_deep_groups_end_instead_of_recursing(tmp_path):
    objs = {
        10: (10000, _msg((4, _ref(20)))),
        20: (2001, _anchored(["Self", "Cycle", "Deep"], [30, 31, 1000])),
        30: (2003, _msg((1, _ref(30)))),
        31: (2003, _msg((1, _ref(32)))),
        32: (2003, _msg((1, _ref(31)))),
    }
    # More groups inside groups than Python's call stack holds
    for oid in range(1000, 2200):
        objs[oid] = (3008, _msg((2, _ref(oid + 1))))
    objs[2200] = (2011, _msg((2, _ref(2201))))
    objs[2201] = (2001, _msg((3, "Buried")))
    out = _melt(_bundle(tmp_path / "doc.pages", objs))
    assert [b.text for b in out.blocks] == ["1| Self\n2| Cycle\n3| Deep\n4| End"]
    assert out.needs == ["1 iwork drawable not read (nested past 64 levels)"]


def test_bundle_folder_never_follows_links(tmp_path):
    zipped = _bundle(tmp_path / "other.pages", _pages_objects(), {"Data/pic.png": PNG})
    with zipfile.ZipFile(zipped) as z:
        z.extractall(tmp_path / "elsewhere")
    folder = tmp_path / "doc.pages"
    (folder / "Index").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    (folder / "Index" / "Document.iwa").symlink_to(elsewhere / "Index" / "Document.iwa")
    (folder / "Data").symlink_to(elsewhere / "Data", target_is_directory=True)
    out = _melt(folder)
    assert not out.blocks and not out.jobs
    assert out.needs[0] == "bundle links not followed: Data, Index/Document.iwa"


def test_keynote_pictures_are_numbered_within_their_slide(tmp_path):
    objs = {
        1: (1, _msg((2, _ref(2)))),
        2: (2, _msg((3, _msg((2, _ref(3)), (2, _ref(4)))))),
        3: (4, _msg((2, _ref(10)))),
        4: (4, _msg((2, _ref(11)))),
        10: (5, _msg((5, _ref(20)), (7, _ref(50)))),
        11: (5, _msg((5, _ref(21)), (7, _ref(51)))),
        20: (7, _msg((1, _msg((2, _ref(40)))))),
        21: (7, _msg((1, _msg((2, _ref(41)))))),
        40: (2001, _msg((3, "One"))),
        41: (2001, _msg((3, "Two"))),
        50: (3005, _msg((15, _ref(7)))),
        51: (3005, _msg((15, _ref(8)))),
        60: (11006, _msg((4, _msg((1, 7), (3, "a.png"))), (4, _msg((1, 8), (3, "b.png"))))),
    }
    files = {"Data/a.png": PNG, "Data/b.png": PNG + b"b"}
    out = _melt(_bundle(tmp_path / "deck.key", objs, files))
    assert [j.src.cite() for j in out.jobs] == ["deck.key#slide1#img1", "deck.key#slide2#img1"]


def test_iwork09_pages_numbers_only_the_body_flow(tmp_path):
    sf = "http://developer.apple.com/namespaces/sf"
    sfa = "http://developer.apple.com/namespaces/sfa"
    sl = "http://developer.apple.com/namespaces/sl"
    table = (
        '<sf:attachments><sf:attachment sfa:ID="T-0"><sf:tabular-info><sf:tabular-model>'
        '<sf:grid sf:numcols="2"><sf:datasource><sf:t><sf:ct sfa:s="A"/></sf:t><sf:n sf:v="7"/>'
        "</sf:datasource></sf:grid></sf:tabular-model></sf:tabular-info></sf:attachment>"
        "</sf:attachments>"
    )
    xml = f"""<sl:document xmlns:sl="{sl}" xmlns:sf="{sf}" xmlns:sfa="{sfa}">
      <sl:section-prototypes><sl:prototype><sf:text-storage sf:kind="body"><sf:text-body>
        <sf:p>Template sample</sf:p>
      </sf:text-body></sf:text-storage></sl:prototype></sl:section-prototypes>
      <sl:drawables><sl:page-group><sf:drawable-shape><sf:text>
        <sf:text-storage sf:kind="textbox"><sf:text-body><sf:p>Floating box</sf:p>
        </sf:text-body></sf:text-storage></sf:text></sf:drawable-shape></sl:page-group>
      </sl:drawables>
      <sf:text-storage sf:kind="body">{table}<sf:text-body><sf:section><sf:layout>
        <sf:p>First</sf:p><sf:p/><sf:p>See below<sf:attachment-ref sfa:IDREF="T-0"/></sf:p>
        <sf:p>Last</sf:p>
      </sf:layout></sf:section></sf:text-body></sf:text-storage></sl:document>"""
    path = tmp_path / "old.pages"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("index.xml", xml)
    assert [(b.src.cite(), b.text) for b in _melt(path).blocks] == [
        ("old.pages:1", "1| First\n3| See below"),
        ("old.pages:3", "| row | A | B |\n|---|---|---|\n| 1 | A | 7 |"),
        # A text box outside the flow continues the count after the body
        ("old.pages:4", "4| Last\n5| Floating box"),
    ]


def test_iwork09_deep_xml_reads_without_recursing(tmp_path):
    sf = "http://developer.apple.com/namespaces/sf"
    # Far past Python's recursion limit, in both the wrappers and inside the paragraph
    deep = 5000
    para = "<sf:p>" + "<sf:span>" * deep + "Deep text" + "</sf:span>" * deep + "</sf:p>"
    xml = (
        f'<sl:document xmlns:sl="x" xmlns:sf="{sf}"><sf:text-storage sf:kind="body">'
        f"<sf:text-body>{'<sf:section>' * deep}{para}{'</sf:section>' * deep}"
        "</sf:text-body></sf:text-storage></sl:document>"
    )
    path = tmp_path / "old.pages"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("index.xml", xml)
    out = _melt(path)
    assert [(b.src.cite(), b.text) for b in out.blocks] == [("old.pages:1", "1| Deep text")]
    assert out.needs == []
