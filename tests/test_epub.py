import importlib.util
import zipfile
from pathlib import Path

import pytest

from meltify.converters import epub
from meltify.evidence import Src
from tests.fixtures.make_docs import card

PICTURE = card("CHAPTER CHART 42", (300, 100))
CONTAINER = (
    '<?xml version="1.0"?><container version="1.0" '
    'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
    '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
    "</rootfiles></container>"
)
OPF = (
    '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
    '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Field Notes</dc:title>'
    "</metadata><manifest>"
    '<item id="cover" href="Text/cover.xhtml" media-type="application/xhtml+xml"/>'
    '<item id="ch1" href="Text/chapter%201.xhtml" media-type="application/xhtml+xml"/>'
    '<item id="gone" href="Text/gone.xhtml" media-type="application/xhtml+xml"/>'
    '</manifest><spine><itemref idref="cover"/><itemref idref="ch1"/><itemref idref="gone"/>'
    "</spine></package>"
)
XHTML = (
    '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" '
    'xmlns:xlink="http://www.w3.org/1999/xlink"><body>{}</body></html>'
)


def _book(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", CONTAINER)
        z.writestr("OEBPS/content.opf", OPF)
        z.writestr(
            "OEBPS/Text/cover.xhtml",
            XHTML.format('<svg><image xlink:href="../Images/cover.png"/></svg>'),
        )
        z.writestr(
            "OEBPS/Text/chapter 1.xhtml",
            XHTML.format(
                '<h1 id="intro">Intro</h1><p>Rain fell on the field.</p>'
                '<img src="../Images/cover.png"/><img src="../Images/none.png"/>'
                '<img src="https://example.org/x.png"/><img src="../../../etc/passwd"/>'
            ),
        )
        z.writestr("OEBPS/Images/cover.png", PICTURE)
    return path


def test_spine_pictures_cite_section_anchor_and_index(tmp_path):
    out = epub.convert(_book(tmp_path / "b.epub"), Src("b.epub"))
    assert [j.src.cite() for j in out.jobs] == ["b.epub#s1#img1", "b.epub#intro#s2#img1"]
    assert all(j.data == PICTURE for j in out.jobs)
    needs = sorted(n for n in out.needs if n != "markitdown")
    assert needs == [
        "1 missing chapter not read",
        "1 remote image not read",
        "2 missing images not read",
    ]


@pytest.mark.skipif(importlib.util.find_spec("markitdown") is None, reason="needs markitdown")
def test_book_text_comes_from_markitdown(tmp_path, monkeypatch):
    import markitdown

    # The epub converter runs straight, without the wrapper's file type guessing
    monkeypatch.setattr(markitdown, "MarkItDown", lambda *a, **k: pytest.fail("built MarkItDown"))
    out = epub.convert(_book(tmp_path / "b.epub"), Src("b.epub"))
    (block,) = out.blocks
    assert block.src.cite() == "b.epub"
    assert "Field Notes" in block.text and "Rain fell on the field." in block.text


def test_oversized_spine_document_is_refused_before_reading(tmp_path, monkeypatch):
    monkeypatch.setattr(epub, "MAX_PART_BYTES", 100)
    with pytest.raises(ValueError, match="limit"):
        epub.convert(_book(tmp_path / "b.epub"), Src("b.epub"))


def test_spine_document_that_inflates_like_a_zip_bomb_is_refused(tmp_path):
    book = _book(tmp_path / "book.epub")
    path = tmp_path / "b.epub"
    with zipfile.ZipFile(book) as src, zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for info in src.infolist():
            data = src.read(info)
            if info.filename == "OEBPS/Text/cover.xhtml":
                data = XHTML.format(" " * (2 << 20)).encode()
            z.writestr(info.filename, data)
    with pytest.raises(ValueError, match="zip bomb"):
        epub.convert(path, Src("b.epub"))


def test_oversized_picture_is_listed_not_read(tmp_path, monkeypatch):
    path = _book(tmp_path / "b.epub")
    monkeypatch.setattr("meltify.converters.embeds.MAX_PART_BYTES", len(PICTURE) - 1)
    monkeypatch.setattr(epub, "MAX_TEXT_BYTES", 10_000)
    with zipfile.ZipFile(path) as z:
        embeds = epub.pictures(z, epub.spine(z), Src("b.epub"))
    assert embeds.jobs == []
    assert embeds.skipped["oversized image"] == 2


def _repeats(path: Path, names: list[str], times: int) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("META-INF/container.xml", CONTAINER)
        z.writestr("OEBPS/content.opf", OPF)
        imgs = "".join(f'<img src="../Images/{n}"/>' for n in names) * times
        z.writestr("OEBPS/Text/chapter 1.xhtml", XHTML.format(imgs))
        for i, n in enumerate(names):
            z.writestr(f"OEBPS/Images/{n}", PICTURE + bytes([i]) * 4000)
    return path


def test_a_picture_shown_many_times_is_unpacked_once(tmp_path, monkeypatch):
    path = _repeats(tmp_path / "b.epub", ["a.png"], 40)
    reads = []
    real = zipfile.ZipFile.read

    def read(self, name, pwd=None):
        reads.append(getattr(name, "filename", name))
        return real(self, name, pwd)

    monkeypatch.setattr(zipfile.ZipFile, "read", read)
    out = epub.convert(path, Src("b.epub"))
    assert reads.count("OEBPS/Images/a.png") == 1
    assert len(out.jobs) == 40
    # One copy in memory, however many places cite it
    assert len({id(j.data) for j in out.jobs}) == 1


def test_pictures_past_the_book_budget_are_needs(tmp_path, monkeypatch):
    path = _repeats(tmp_path / "b.epub", ["a.png", "b.png", "c.png"], 1)
    # Room for two of the three pictures
    monkeypatch.setattr("meltify.converters.embeds.MAX_PICTURE_BYTES", 2 * (len(PICTURE) + 4000))
    out = epub.convert(path, Src("b.epub"))
    assert len(out.jobs) == 2
    assert "1 image not read (over the 256 MiB picture total)" in out.needs
