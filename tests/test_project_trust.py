import pytest

from meltify import config

NETWORK = """
[llm]
openai_base_url = "http://192.0.2.1/v1"
openai_key_env = "ACME_TOKEN"
[ocr]
engines = "openai"
dpi = 200
[ocr.endpoints.served]
base_url = "http://192.0.2.1/v1"
model = "acme"
key_env = "ACME_TOKEN"
[asr]
engine = "api"
lang = "en"
[asr.endpoints.served]
base_url = "http://192.0.2.1/v1"
model = "acme"
key_env = "ACME_TOKEN"
"""


def test_discovered_project_cannot_select_network_or_credentials(tmp_path):
    project = tmp_path / "meltify.toml"
    project.write_text(NETWORK)
    s = config.load(cwd=tmp_path, env={"XDG_CONFIG_HOME": str(tmp_path / "xdg")})
    assert s["llm"] == config.defaults()["llm"]
    assert s["ocr"]["engines"] == s["asr"]["engine"] == "auto"
    assert s["ocr"]["endpoints"] == s["asr"]["endpoints"] == {}
    assert s["ocr"]["dpi"] == 200
    assert s["asr"]["lang"] == "en"
    assert len(s["_warnings"]) == 5
    for warning in s["_warnings"]:
        assert "--config" in warning
        assert "192.0.2.1" not in warning
        assert "ACME_TOKEN" not in warning


@pytest.mark.parametrize("layer", ["user", "explicit", "override"])
def test_network_settings_from_trusted_layers_are_preserved(tmp_path, layer):
    project = tmp_path / "meltify.toml"
    project.write_text(NETWORK)
    env = {"XDG_CONFIG_HOME": str(tmp_path / "xdg")}
    if layer == "user":
        user = tmp_path / "xdg/meltify/config.toml"
        user.parent.mkdir(parents=True)
        user.write_text(NETWORK)
    s = config.load(
        {"llm": {"openai_key_env": "ACME_TOKEN"}} if layer == "override" else None,
        cwd=tmp_path,
        env=env,
        project=project if layer == "explicit" else None,
    )
    assert s["llm"]["openai_key_env"] == "ACME_TOKEN"
    if layer != "override":
        assert s["ocr"]["endpoints"]["served"]["model"] == "acme"
    if layer == "explicit":
        assert s["_warnings"] == []


def test_discovered_project_cannot_redirect_an_existing_user_endpoint(tmp_path):
    user = tmp_path / "xdg/meltify/config.toml"
    user.parent.mkdir(parents=True)
    user.write_text(NETWORK.replace("192.0.2.1", "192.0.2.2"))
    (tmp_path / "meltify.toml").write_text(NETWORK)
    s = config.load(cwd=tmp_path, env={"XDG_CONFIG_HOME": str(tmp_path / "xdg")})
    assert s["ocr"]["endpoints"]["served"]["base_url"] == "http://192.0.2.2/v1"
    assert s["llm"]["openai_base_url"] == "http://192.0.2.2/v1"


@pytest.mark.parametrize("text", ['ocr = "broken"', 'asr = "broken"', 'llm = "broken"'])
def test_discovered_invalid_network_section_is_ignored(tmp_path, text):
    (tmp_path / "meltify.toml").write_text(text)
    s = config.load(cwd=tmp_path, env={"XDG_CONFIG_HOME": str(tmp_path / "xdg")})
    assert s["ocr"] == config.defaults()["ocr"]
    assert s["asr"] == config.defaults()["asr"]
    assert s["llm"] == config.defaults()["llm"]
    assert len(s["_warnings"]) == 1
