import json

import pytest

from meltify.cli import main
from meltify.files import parse_pages
from meltify.forensics.pdf import luminance, scan
from tests.fixtures.make_pdf import HIDDEN, VISIBLE, hidden_pdf, image_text_pdf, white_on_image_pdf


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


def test_cli_cites_page_and_box(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    pdf = hidden_pdf(tmp_path / "h.pdf")
    assert main(["hidden", str(pdf), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    cites = [r["cite"] for r in out["results"]]
    assert all(c.startswith(f"{pdf}#p1@pt(") for c in cites)
    assert len(cites) == len(HIDDEN)


def test_contrast_renders_pages_for_text_inside_images(tmp_path, monkeypatch, capsys):
    import numpy as np
    from PIL import Image

    monkeypatch.chdir(tmp_path)
    pdf = image_text_pdf(tmp_path / "img.pdf")
    assert main(["hidden", str(pdf), "--contrast", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["results"] == []
    image = Image.open(out["artifacts"][0]["path"]).convert("L")
    # The faint text becomes visible as a wide spread of tones
    assert np.asarray(image).std() > 20


def test_parse_pages():
    assert parse_pages(None, 3) == [1, 2, 3]
    assert parse_pages("1,3-5", 6) == [1, 3, 4, 5]
    assert parse_pages("2,2", 3) == [2]


@pytest.mark.parametrize("spec", ["9", "1,3-5,9", "0", "5-3", "1,", "", "a", "1-"])
def test_parse_pages_rejects_bad_specs(spec):
    with pytest.raises(ValueError):
        parse_pages(spec, 6)


@pytest.mark.parametrize("spec", ["9", "2-1", "1,"])
def test_cli_rejects_pages_it_cannot_select(tmp_path, monkeypatch, capsys, spec):
    monkeypatch.chdir(tmp_path)
    pdf = hidden_pdf(tmp_path / "h.pdf")
    assert main(["hidden", str(pdf), "--pages", spec, "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["results"] == []


def test_empty_page_selection_is_not_all_pages(tmp_path):
    with pytest.raises(ValueError):
        scan(str(hidden_pdf(tmp_path / "h.pdf")), [])


def test_white_text_over_dark_image_is_not_hidden(tmp_path):
    assert scan(str(white_on_image_pdf(tmp_path / "w.pdf"))) == []
