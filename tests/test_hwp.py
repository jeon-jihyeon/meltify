import base64
import hashlib
import importlib.util
import io
import json
import os
import shutil
import zipfile
import zlib
from pathlib import Path

import pytest

from meltify import passwords
from meltify.cli import main
from meltify.converters import hwp, pick
from meltify.converters.hwp import convert
from meltify.evidence import Src
from meltify.passwords import NEEDS_CRYPTO, NEEDS_DRM

SAMPLES = Path(__file__).parent / "fixtures" / "samples" / "docs"


@pytest.fixture(autouse=True)
def no_password(monkeypatch):
    monkeypatch.delenv("MELTIFY_PASSWORD", raising=False)


def _melt(name: str):
    path = SAMPLES / name
    return convert(path, Src(name))


def _shut(path: Path) -> str:
    """The needs line of a file that stays shut"""
    with pytest.raises(passwords.Locked) as e:
        convert(path, Src(path.name))
    return str(e.value)


def test_hwp_table_cites_section_and_paragraph():
    out = _melt("hwplib-table.hwp")
    md = out.markdown("t")
    assert "## hwplib-table.hwp#s1:1\n| ABC 123 | DEF | GHI |\n|---|---|---|" in md
    assert "| UVM | 123 | 456 |" in md
    # The second table has only blank cells, so it adds nothing to quote
    assert len(out.blocks) == 1


def test_hwpx_merged_cells_show_once_at_their_anchor():
    md = _melt("hwpxlib-SimpleTable.hwpx").markdown("t")
    assert "| 1 |  | 2 |\n|---|---|---|\n|  |  | 3 |\n| 5 | 4 |  |" in md


def test_hwpx_paragraphs_are_numbered_by_position():
    out = _melt("hwpxlib-sample1.hwpx")
    assert out.blocks[0].src.cite() == "hwpxlib-sample1.hwpx#s1:1"
    assert out.blocks[0].text.splitlines()[0] == "1| 수학"


def test_hwp_picture_becomes_one_image_job():
    out = _melt("hwplib-picture.hwp")
    # The sample draws one picture four times, and only the first placement is OCR'd
    assert [j.src.cite() for j in out.jobs] == ["hwplib-picture.hwp#s1#img1"]
    assert out.jobs[0].kind == "image"
    assert out.jobs[0].data.startswith(b"\x89PNG")


def test_distribution_hwp_decrypts_without_a_password():
    pytest.importorskip("cryptography")
    out = _melt("hwplib-distribution.hwp")
    first = out.blocks[0]
    assert first.src.cite() == "hwplib-distribution.hwp#s1:1"
    assert first.text.splitlines()[:2] == [
        " 1| 강남세움복지관 공고 제 2024-08호",
        " 3| 2025년 강남세움센터 시설관리원 용역업체 선정 입찰공고",
    ]
    assert not out.needs


def test_distribution_hwp_without_cryptography_names_the_extra(monkeypatch):
    real = importlib.util.find_spec

    def find_spec(name, *args):
        return None if name == "cryptography" else real(name, *args)

    monkeypatch.setattr(hwp.importlib.util, "find_spec", find_spec)
    assert _shut(SAMPLES / "hwplib-distribution.hwp") == NEEDS_CRYPTO


def _cfb1_encrypt(key: bytes, data: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    encrypt = Cipher(algorithms.AES(key), modes.ECB()).encryptor().update
    register, out = 0, bytearray()
    for byte in data:
        sealed = 0
        for shift in range(7, -1, -1):
            bit = ((byte >> shift) & 1) ^ (encrypt(register.to_bytes(16, "big"))[0] >> 7)
            sealed |= bit << shift
            register = ((register << 1) | bit) & ((1 << 128) - 1)
        out.append(sealed)
    return bytes(out)


def _password_hwp(
    tmp_path: Path, secret: str, sample: str = "hwplib-table.hwp", edit=None, **flags: bool
) -> Path:
    """A sample sealed the way Hancom seals a password document"""
    from dataclasses import replace

    from hwpx.hwp5.cfb import CompoundFile, build_compound_file, read_all_streams
    from hwpx.hwp5.fileheader import FLAG_BITS, parse_file_header

    compound = CompoundFile((SAMPLES / sample).read_bytes())
    header = parse_file_header(compound.read("FileHeader"))
    streams = dict(read_all_streams(compound))
    if edit is not None:
        edit(streams)
    key = hwp._password_key(secret)
    for name in streams:
        if name == "DocInfo" or name.startswith(("BodyText/", "BinData/")):
            streams[name] = _cfb1_encrypt(key, streams[name])
    bits = header.flags | 1 << FLAG_BITS["password"]
    for name, on in flags.items():
        bits |= on << FLAG_BITS[name]
    streams["FileHeader"] = replace(header, flags=bits, encrypt_version=4).to_bytes()
    path = tmp_path / "locked.hwp"
    path.write_bytes(build_compound_file(streams.items()))
    return path


def test_cfb1_matches_the_published_vector():
    pytest.importorskip("cryptography")
    # From rhwp's password-crypto tests, password "helloworld" over bytes 0..31
    plain = hwp._cfb1(hwp._password_key("helloworld"), bytes(range(32)))
    assert plain.hex() == "00013eec903dbc26faff9c6cfb354800bcaa147b0ed15c32211737fa971de379"


def test_password_hwp_opens_with_the_password(tmp_path, monkeypatch):
    pytest.importorskip("cryptography")
    path = _password_hwp(tmp_path, "비밀 123")
    assert _shut(path) == passwords.LOCKED
    monkeypatch.setenv("MELTIFY_PASSWORD", "비밀 123")
    out = convert(path, Src("t.hwp"))
    plain = _melt("hwplib-table.hwp")
    assert [b.text for b in out.blocks] == [b.text for b in plain.blocks] and not out.needs
    monkeypatch.setenv("MELTIFY_PASSWORD", "비밀 124")
    assert _shut(path) == passwords.WRONG


def _stored_but_deflated(streams: dict[str, bytes]) -> None:
    """Deflate each picture while its BinData record says stored, as Hancom writes GIFs
    in a password document"""
    docinfo, at = bytearray(streams["DocInfo"]), 0
    while at < len(docinfo):
        word = int.from_bytes(docinfo[at : at + 4], "little")
        tag, size, at = word & 0x3FF, word >> 20, at + 4
        if size == 0xFFF:
            size, at = int.from_bytes(docinfo[at : at + 4], "little"), at + 4
        if tag == 18:  # BIN_DATA, whose props bits 4 and 5 hold the compression
            props = int.from_bytes(docinfo[at : at + 2], "little") & ~0x30 | 0x20
            docinfo[at : at + 2] = props.to_bytes(2, "little")
        at += size
    streams["DocInfo"] = bytes(docinfo)
    for name in [n for n in streams if n.startswith("BinData/")]:
        packer = zlib.compressobj(9, zlib.DEFLATED, -15)
        # Hancom leaves a few bytes of padding after the stream
        streams[name] = packer.compress(streams[name]) + packer.flush() + bytes(40)


def test_password_hwp_picture_flagged_stored_is_inflated(tmp_path, monkeypatch):
    pytest.importorskip("cryptography")
    path = _password_hwp(tmp_path, "pw", "hwplib-picture.hwp", _stored_but_deflated)
    monkeypatch.setenv("MELTIFY_PASSWORD", "pw")
    out = convert(path, Src("p.hwp"))
    assert [j.src.cite() for j in out.jobs] == ["p.hwp#s1#img1"]
    assert out.jobs[0].data.startswith(b"\x89PNG") and not out.needs


def test_drm_hwp_points_to_the_drm_client(tmp_path):
    fasoo = tmp_path / "fasoo.hwp"
    fasoo.write_bytes(b"\x9b DRMONE  This Document is encrypted and protected by Fasoo DRM")
    markany = tmp_path / "markany.hwp"
    markany.write_bytes(b"\x00" * 16 + b"MarkAny Document SAFER" + os.urandom(64))
    pytest.importorskip("cryptography")
    # A DRM flag in the FileHeader wins over the password the file also carries
    flagged = _password_hwp(tmp_path, "x", drm=True)
    for path in (fasoo, markany, flagged):
        assert _shut(path) == NEEDS_DRM


def _password_hwpx(path: Path, secret: str, section: bytes | None = None, extra=()) -> Path:
    """A generated HWPX with its section sealed by ODF package encryption"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from hwpx import HwpxDocument

    doc = HwpxDocument.new()
    doc.paragraphs[0].text = "잠긴 문단"
    buf = io.BytesIO()
    doc.save_to_stream(buf)
    salt, iv = os.urandom(16), os.urandom(16)
    key = hashlib.pbkdf2_hmac("sha1", hashlib.sha256(secret.encode()).digest(), salt, 1024, 32)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    with zipfile.ZipFile(buf) as src, zipfile.ZipFile(path, "w") as z:
        for info in src.infolist():
            data = src.read(info)
            if info.filename == "Contents/section0.xml":
                data = data if section is None else section
                check = hashlib.sha256(data[:1024]).digest()
                packed = zlib.compressobj(9, zlib.DEFLATED, -15)
                body = packed.compress(data) + packed.flush()
                body += b"\0" * (-len(body) % 16)
                e = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
                data = e.update(body) + e.finalize()
            elif info.filename == "META-INF/manifest.xml":
                data = (
                    '<?xml version="1.0" encoding="UTF-8"?><odf:manifest xmlns:odf="urn:x">'
                    '<odf:file-entry full-path="Contents/section0.xml">'
                    f'<odf:encryption-data checksum-type="sha256-1k" checksum="{b64(check)}">'
                    '<odf:algorithm algorithm-name="http://www.w3.org/2001/04/xmlenc#aes256-cbc"'
                    f' initialisation-vector="{b64(iv)}"/>'
                    f'<odf:key-derivation key-size="32" iteration-count="1024" salt="{b64(salt)}"/>'
                    "</odf:encryption-data></odf:file-entry></odf:manifest>"
                ).encode()
            z.writestr(info, data)
        for name, data in extra:
            z.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)
    return path


def test_password_hwpx_opens_with_the_password(tmp_path, monkeypatch):
    pytest.importorskip("cryptography")
    path = _password_hwpx(tmp_path / "locked.hwpx", "s3cr3t")
    assert _shut(path) == passwords.LOCKED
    monkeypatch.setenv("MELTIFY_PASSWORD", "s3cr3t")
    out = convert(path, Src("l.hwpx"))
    assert [(b.src.cite(), b.text) for b in out.blocks] == [("l.hwpx#s1:1", "1| 잠긴 문단")]
    monkeypatch.setenv("MELTIFY_PASSWORD", "nope")
    assert _shut(path) == passwords.WRONG


def test_password_hwpx_part_that_inflates_past_the_limit_is_a_need(tmp_path, monkeypatch):
    pytest.importorskip("cryptography")
    monkeypatch.setattr(hwp, "MAX_PART_BYTES", 1 << 20)
    # A few KB of ciphertext that inflates to 8 MB once the password opens it
    bomb = b"<hs:sec/>" + b" " * (8 << 20)
    path = _password_hwpx(tmp_path / "locked.hwpx", "s3cr3t", section=bomb)
    assert path.stat().st_size < 64 << 10
    monkeypatch.setenv("MELTIFY_PASSWORD", "s3cr3t")
    out = convert(path, Src("l.hwpx"))
    assert out.needs == ["hwpx part Contents/section0.xml over the size limit"]
    assert not out.blocks


def test_password_hwpx_plain_part_past_the_limit_is_a_need(tmp_path, monkeypatch):
    pytest.importorskip("cryptography")
    monkeypatch.setattr(hwp, "MAX_PART_BYTES", 1 << 20)
    path = _password_hwpx(
        tmp_path / "locked.hwpx", "s3cr3t", extra=[("BinData/z.bmp", bytes(4 << 20))]
    )
    monkeypatch.setenv("MELTIFY_PASSWORD", "s3cr3t")
    out = convert(path, Src("l.hwpx"))
    assert out.needs == ["hwpx part BinData/z.bmp over the size limit"]


def _swap_picture(ext: str, data: bytes):
    """Make the sample's one PNG a picture of another kind, as Hancom names it"""

    def edit(streams: dict[str, bytes]) -> None:
        streams["DocInfo"] = streams["DocInfo"].replace(
            "png".encode("utf-16-le"), ext.encode("utf-16-le")
        )
        del streams["BinData/BIN0001.png"]
        streams[f"BinData/BIN0001.{ext}"] = data

    return edit


def _hwp_with(tmp_path: Path, edit) -> Path:
    from hwpx.hwp5.cfb import CompoundFile, build_compound_file, read_all_streams

    streams = dict(read_all_streams(CompoundFile((SAMPLES / "hwplib-picture.hwp").read_bytes())))
    edit(streams)
    path = tmp_path / "p.hwp"
    path.write_bytes(build_compound_file(streams.items()))
    return path


def _wmf_label() -> bytes:
    from tests.fixtures.make_binary import wmf, wmf_textout

    return wmf(wmf_textout(0, 0, b"Q3 revenue 4,210"))


def test_hwp_wmf_picture_gives_its_text(tmp_path, monkeypatch):
    from meltify.converters import render

    monkeypatch.setattr(render, "available", lambda: pytest.fail("text records need no render"))
    path = _hwp_with(tmp_path, _swap_picture("wmf", _wmf_label()))
    out = convert(path, Src("p.hwp"))
    assert [(b.src.cite(), b.text) for b in out.blocks if "Q3" in b.text] == [
        ("p.hwp#s1#img1", "Q3 revenue 4,210")
    ]
    assert (out.jobs, out.needs) == ([], [])


def test_hwp_ole_object_is_a_need(tmp_path):
    path = _hwp_with(tmp_path, _swap_picture("ole", b"\xd0\xcf\x11\xe0 object"))
    out = convert(path, Src("p.hwp"))
    assert out.jobs == []
    assert out.needs == ["1 embedded object not read"]


def test_password_hwp_wmf_picture_is_decrypted_and_read(tmp_path, monkeypatch):
    pytest.importorskip("cryptography")
    from meltify.converters import render

    monkeypatch.setattr(render, "available", lambda: pytest.fail("text records need no render"))
    path = _password_hwp(tmp_path, "pw", "hwplib-picture.hwp", _swap_picture("wmf", _wmf_label()))
    monkeypatch.setenv("MELTIFY_PASSWORD", "pw")
    out = convert(path, Src("p.hwp"))
    assert [b.text for b in out.blocks if "Q3" in b.text] == ["Q3 revenue 4,210"]
    assert out.needs == []


def test_generated_hwpx_keeps_empty_paragraphs_in_the_count(tmp_path):
    from hwpx import HwpxDocument

    doc = HwpxDocument.new()
    doc.paragraphs[0].text = "첫 줄"
    doc.add_paragraph("")
    table = doc.add_table(2, 2)
    table.set_cell_text(0, 0, "키")
    table.set_cell_text(1, 1, "값|x")
    doc.add_paragraph("끝 문단")
    buf = io.BytesIO()
    doc.save_to_stream(buf)
    path = tmp_path / "g.hwpx"
    path.write_bytes(buf.getvalue())

    cites = [(b.src.cite(), b.text) for b in convert(path, Src("g.hwpx")).blocks]
    assert cites == [
        ("g.hwpx#s1:1", "1| 첫 줄"),
        ("g.hwpx#s1:3", "| 키 |  |\n|---|---|\n|  | 값\\|x |"),
        ("g.hwpx#s1:4", "4| 끝 문단"),
    ]


def test_read_cites_hwp_end_to_end(tmp_path, monkeypatch, capsys):
    for name in ("hwplib-table.hwp", "hwplib-distribution.hwp"):
        shutil.copy(SAMPLES / name, tmp_path / name)
    assert pick(tmp_path / "a.hwpx")[0] == "hwp"
    monkeypatch.chdir(tmp_path)
    main(["read", "hwplib-table.hwp", "hwplib-distribution.hwp", "--json", "--limit", "0"])
    rows = {r["cite"]: r for r in json.loads(capsys.readouterr().out)["results"]}
    md = Path(rows["hwplib-table.hwp"]["out"]).read_text()
    assert "## hwplib-table.hwp#s1:1\n" in md
    crypto = importlib.util.find_spec("cryptography") is not None
    assert rows["hwplib-distribution.hwp"]["needs"] == ([] if crypto else [NEEDS_CRYPTO])


@pytest.mark.parametrize(
    ("code", "need"),
    [
        ("hwp5-password", passwords.LOCKED),
        ("hwp5-distribution", hwp.SEALED["hwp5-distribution"]),
        ("hwp5-drm", NEEDS_DRM),
    ],
)
def test_lock_the_header_checks_miss_is_never_rendered(tmp_path, monkeypatch, capsys, code, need):
    from hwpx import HwpxDocument
    from hwpx.hwp5.errors import Hwp5Error

    from meltify.converters import fallback

    def refuse(data):
        raise Hwp5Error("protected", code=code)

    shutil.copy(SAMPLES / "hwplib-table.hwp", tmp_path / "t.hwp")
    monkeypatch.setattr(HwpxDocument, "open", refuse)
    monkeypatch.setattr(fallback, "convert", lambda *a, **k: pytest.fail("rendered a lock"))
    monkeypatch.chdir(tmp_path)
    main(["read", "t.hwp", "--json", "--limit", "0"])
    [row] = json.loads(capsys.readouterr().out)["results"]
    assert row["needs"] == [need] and "error" not in row
