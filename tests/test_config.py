import json
from pathlib import Path

import pytest

from meltify import cli, config


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def home(tmp_path):
    return {"XDG_CONFIG_HOME": str(tmp_path / "xdg")}


def test_defaults_only(tmp_path, home):
    s = config.load(cwd=tmp_path, env=home)
    assert s["lang"] == "ko"
    assert s["ocr"]["upscale"] == 3
    assert s["_sources"] == ["defaults"]


def test_precedence_cli_over_env_over_project_over_user(tmp_path, home):
    _write(tmp_path / "xdg/meltify/config.toml", 'lang = "en"\n[ocr]\nupscale = 2\ndpi = 200\n')
    (tmp_path / "repo/.git").mkdir(parents=True)
    _write(tmp_path / "repo/meltify.toml", "[ocr]\nupscale = 4\n")
    work = tmp_path / "repo/sub/dir"
    work.mkdir(parents=True)
    env = {**home, "MELTIFY_OCR_UPSCALE": "5", "MELTIFY_LANG": "ja"}

    s = config.load({"lang": "fr"}, cwd=work, env=env)

    assert s["lang"] == "fr"
    assert s["ocr"]["upscale"] == 5
    assert s["ocr"]["dpi"] == 200
    assert s["_sources"][1].endswith("xdg/meltify/config.toml")
    assert s["_sources"][2].endswith("repo/meltify.toml")
    assert s["_sources"][3:] == ["env", "cli"]


def test_project_search_stops_at_git_root(tmp_path, home):
    _write(tmp_path / "meltify.toml", 'lang = "en"\n')
    (tmp_path / "repo/.git").mkdir(parents=True)
    s = config.load(cwd=tmp_path / "repo", env=home)
    assert s["lang"] == "ko"


def test_project_search_without_git_reads_only_cwd(tmp_path, home):
    _write(tmp_path / "meltify.toml", 'lang = "en"\n')
    (tmp_path / "work").mkdir()
    assert config.load(cwd=tmp_path / "work", env=home)["lang"] == "ko"
    assert config.load(cwd=tmp_path, env=home)["lang"] == "en"


@pytest.mark.parametrize(
    "name,raw,section,key,want",
    [
        ("MELTIFY_OCR_SHARPEN", "false", "ocr", "sharpen", False),
        ("MELTIFY_MEDIA_FPS", "2", "media", "fps", 2.0),
        ("MELTIFY_MEDIA_SUB_LANGS", "en, ja", "media", "sub_langs", ["en", "ja"]),
        ("MELTIFY_SUBMIT_MAX_ATTEMPTS", "7", "submit", "max_attempts", 7),
    ],
)
def test_env_values_take_the_default_type(tmp_path, home, name, raw, section, key, want):
    s = config.load(cwd=tmp_path, env={**home, name: raw})
    assert s[section][key] == want


def test_unknown_env_keys_are_ignored(tmp_path, home):
    s = config.load(cwd=tmp_path, env={**home, "MELTIFY_NOPE": "1", "MELTIFY_OCR_NOPE": "1"})
    assert "nope" not in s
    assert "nope" not in s["ocr"]


def test_explicit_config_path_and_errors(tmp_path, home):
    p = _write(tmp_path / "custom.toml", "[submit]\ngap = 2.5\n")
    assert config.load(cwd=tmp_path, env=home, project=p)["submit"]["gap"] == 2.5
    with pytest.raises(config.ConfigError):
        config.load(cwd=tmp_path, env=home, project=tmp_path / "missing.toml")
    bad = _write(tmp_path / "bad.toml", "lang = \n")
    with pytest.raises(config.ConfigError):
        config.load(cwd=tmp_path, env=home, project=bad)


def test_none_override_keeps_lower_layer(tmp_path, home):
    s = config.load({"ocr": {"upscale": None, "dpi": 150}}, cwd=tmp_path, env=home)
    assert s["ocr"]["upscale"] == 3
    assert s["ocr"]["dpi"] == 150


@pytest.mark.parametrize("name,raw", [("MELTIFY_OCR_UPSCALE", "abc"), ("MELTIFY_MEDIA_FPS", "x")])
def test_bad_env_value_is_a_config_error(tmp_path, home, name, raw):
    with pytest.raises(config.ConfigError, match=name):
        config.load(cwd=tmp_path, env={**home, name: raw})


def test_cli_reports_config_error_as_json(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("MELTIFY_OCR_UPSCALE", "abc")
    assert cli.main(["doctor", "--json"]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["errors"][0]["code"] == "usage"
    assert "MELTIFY_OCR_UPSCALE" in out["errors"][0]["message"]


def test_env_sets_keys_that_are_unset_by_default(tmp_path):
    settings = config.load(cwd=tmp_path, env={"MELTIFY_CHECK_COUNT": "3"})
    assert settings["check"]["count"] == 3
