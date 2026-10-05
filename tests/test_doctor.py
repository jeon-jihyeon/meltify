import json

from meltify import safe
from meltify.cli import main
from meltify.commands import doctor


def test_doctor_reports_engines_and_keys_without_values(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GEMINI_API_KEY", "secret-value")
    assert main(["doctor", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    checks = {r["check"]: r for r in out["results"]}
    assert checks["key GEMINI_API_KEY"]["ok"] is True
    assert "secret-value" not in json.dumps(out)
    assert checks["ocr gemini"]["ok"] is True
    assert {"asr mlx", "asr whispercpp", "asr api", "ocr vision", "ocr paddle"} <= set(checks)


def test_missing_base_module_exits_three(monkeypatch, tmp_path):
    import importlib.util

    real = importlib.util.find_spec
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda n, *a: None if n == "jsonschema" else real(n, *a)
    )
    assert main(["doctor", "--quick"]) == 3


def test_engines_tolerate_a_missing_asr_section():
    names = ("claude_model", "anthropic_key_env", "gemini_model", "gemini_key_env")
    llm = {n: "x" for n in (*names, "openai_model", "openai_key_env", "openai_base_url")}
    checks = {r["check"] for r in doctor.engines({"lang": "ko", "llm": llm})}
    assert {"asr mlx", "asr whispercpp", "asr api"} <= checks


def test_data_dir_matches_the_launcher(tmp_path):
    assert doctor.data_dir({"CLAUDE_PLUGIN_DATA": "/p"}).as_posix() == "/p"
    assert doctor.data_dir({"XDG_DATA_HOME": "/x"}).as_posix() == "/x/meltify"


def test_install_creates_venv_and_marker(monkeypatch, tmp_path, capsys):
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        if args[1] == "venv":
            (tmp_path / "data/venv/bin").mkdir(parents=True)
            (tmp_path / "data/venv/bin/python").write_text("")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(safe, "run", fake_run)
    monkeypatch.setattr(safe, "require_binary", lambda name, hint: "uv")
    assert main(["doctor", "--install", "media"]) == 0
    assert calls[0][:2] == ["uv", "venv"]
    assert calls[1][-1].endswith("[media]")
    assert (tmp_path / "data/venv/.meltify-version").read_text() == doctor.__version__
