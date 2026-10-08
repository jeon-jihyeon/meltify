import json
import sys

import pytest

from meltify import __version__, paths, safe, tools
from meltify.cli import main
from meltify.commands import doctor

FETCH = tools.fetch


@pytest.fixture(autouse=True)
def no_downloads(monkeypatch):
    monkeypatch.setattr(tools, "fetch", lambda item, folder: pytest.fail(f"downloaded {item.url}"))


def test_doctor_reports_engines_and_keys_without_values(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GEMINI_API_KEY", "secret-value")
    assert main(["doctor", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    checks = {r["check"]: r for r in out["results"]}
    assert checks["key GEMINI_API_KEY"]["ok"] is True
    assert "secret-value" not in json.dumps(out)
    assert checks["ocr gemini"]["ok"] is True
    assert {"asr whispercpp", "asr api", "ocr vision"} <= set(checks)


def test_missing_base_module_exits_three(monkeypatch, tmp_path):
    import importlib.util

    real = importlib.util.find_spec
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda n, *a: None if n == "openpyxl" else real(n, *a)
    )
    assert main(["doctor", "--quick"]) == 3


def test_engines_list_every_ocr_and_asr_engine():
    from meltify import config
    from meltify.engines import ocr

    checks = {r["check"] for r in doctor.engines(config.defaults())}
    assert {f"ocr {name}" for name in ocr.ENGINES} <= checks
    assert {"asr whispercpp", "asr api"} <= checks


def test_a_bad_endpoint_table_is_a_row_not_a_crash():
    from meltify import config

    settings = config.defaults()
    settings["asr"]["endpoints"] = {"whisper": {"base_url": "http://127.0.0.1:9000/v1"}}
    assert doctor.engines(settings) == [
        doctor._row(
            "config endpoints",
            False,
            "invalid",
            "read images and recordings",
            "asr.endpoints.whisper needs base_url and model",
        )
    ]


def test_an_endpoint_is_ready_only_when_its_server_answers(monkeypatch):
    import httpx

    from meltify import config

    settings = config.defaults()
    settings["ocr"]["endpoints"] = {"vl": {"base_url": "http://127.0.0.1:9/v1", "model": "m"}}

    def down(url, **kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "get", down)
    row = next(r for r in doctor.engines(settings) if r["check"] == "ocr vl")
    assert not row["ok"] and "not answering" in row["hint"]

    def up(url, **kwargs):
        return httpx.Response(200, json={"data": []})

    monkeypatch.setattr(httpx, "get", up)
    assert next(r for r in doctor.engines(settings) if r["check"] == "ocr vl")["ok"]


def test_data_dir_matches_the_launcher(tmp_path):
    assert paths.data_dir({"CLAUDE_PLUGIN_DATA": "/p"}).as_posix() == "/p"
    assert paths.data_dir({"XDG_DATA_HOME": "/x"}).as_posix() == "/x/meltify"


def test_install_creates_venv_and_marker(monkeypatch, tmp_path, capsys):
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        if args[1] == "venv":
            (tmp_path / "data/venv/bin").mkdir(parents=True)
            (tmp_path / "data/venv/bin/python").write_text("")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("MELTIFY_LAUNCHER", "1")
    monkeypatch.setattr(safe, "run", fake_run)
    monkeypatch.setattr(safe, "require_binary", lambda name, hint: "uv")
    assert main(["doctor", "--install", "media"]) == 0
    assert calls[0][:2] == ["uv", "venv"]
    assert calls[1][-1].endswith("[media]")
    assert (tmp_path / "data/venv/.meltify-version").read_text() == doctor.__version__


def test_doctor_lists_the_new_formats(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert main(["doctor", "--json"]) == 0
    checks = {r["check"]: r for r in json.loads(capsys.readouterr().out)["results"]}
    for module in ("trafilatura", "hwpx", "striprtf", "pi_heif"):
        assert checks[f"module {module}"]["ok"] is True
    for module in (
        *("libarchive", "legacy_doc", "python_calamine", "olefile"),
        *("numbers_parser", "pyarrow", "playwright"),
    ):
        assert f"module {module}" in checks
    assert checks["bin soffice"]["used_by"].startswith("read PowerPoint 95, formula recalc")


def test_system_parts_flag_a_binding_without_its_library(monkeypatch):
    import importlib.util

    from meltify.converters import archive, render

    real = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda n, *a: object() if n in ("libarchive", "playwright") else real(n, *a),
    )
    monkeypatch.setattr(archive, "libarchive_ready", lambda: False)
    monkeypatch.setattr(render, "soffice", lambda: None)
    monkeypatch.setattr(doctor, "chrome", lambda: None)
    rows = {r["check"]: r for r in doctor.system_parts()}
    assert rows["bin soffice"]["ok"] is False
    assert "libreoffice" in rows["bin soffice"]["hint"]
    assert rows["lib libarchive"]["ok"] is False
    assert rows["lib libarchive"]["hint"].startswith("brew install libarchive")
    assert rows["browser chrome"]["ok"] is False


def _fake_install(monkeypatch, tmp_path, calls):
    def fake_run(args, **kw):
        calls.append(list(args))
        if args[1] == "venv":
            (tmp_path / "data/venv/bin").mkdir(parents=True)
            (tmp_path / "data/venv/bin/python").write_text("")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("MELTIFY_LAUNCHER", "1")
    monkeypatch.setattr(safe, "run", fake_run)
    monkeypatch.setattr(safe, "require_binary", lambda name, hint: "uv")
    monkeypatch.setattr(tools, "install_seven_zip", lambda root: root / "7zip/7zz")


def test_install_render_downloads_chromium_only_without_chrome(monkeypatch, tmp_path):
    calls: list[list[str]] = []
    _fake_install(monkeypatch, tmp_path, calls)
    monkeypatch.setattr(doctor, "chrome", lambda: None)
    assert main(["doctor", "--install", "render"]) == 0
    assert calls[1][-1].endswith("[render]")
    assert calls[2][1:] == ["-m", "playwright", "install", "chromium"]

    calls.clear()
    monkeypatch.setattr(doctor, "chrome", lambda: "/usr/bin/google-chrome")
    assert main(["doctor", "--install", "render"]) == 0
    assert not any("playwright" in c for c in calls)


def test_install_accepts_the_new_extras(monkeypatch, tmp_path):
    calls: list[list[str]] = []
    _fake_install(monkeypatch, tmp_path, calls)
    for extra in ("archive", "iwork"):
        assert main(["doctor", "--install", extra]) == 0
        assert calls[-1][-1].endswith(f"[{extra}]")


def test_system_parts_report_renderers_and_7zip(monkeypatch, tmp_path):
    from meltify.converters import quicklook, render

    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    portable = tmp_path / "tools/libreoffice/program/soffice"
    monkeypatch.setattr(render, "soffice", lambda: str(portable))
    monkeypatch.setattr(tools, "seven_zip", lambda: None)
    monkeypatch.setattr(quicklook, "available", lambda: True)
    monkeypatch.setattr(doctor, "IS_MAC", True)
    rows = {r["check"]: r for r in doctor.system_parts()}
    assert rows["bin soffice"]["detail"] == f"{portable} (downloaded)"
    assert rows["render quicklook"]["ok"] is True
    assert rows["bin 7zz"]["ok"] is False
    assert rows["bin 7zz"]["hint"] == "meltify doctor --install archive"

    monkeypatch.setattr(doctor, "IS_MAC", False)
    assert "render quicklook" not in {r["check"] for r in doctor.system_parts()}


def test_install_libreoffice_downloads_without_touching_the_venv(monkeypatch, tmp_path, capsys):
    calls: list[list[str]] = []
    _fake_install(monkeypatch, tmp_path, calls)
    got = []
    monkeypatch.setattr(tools, "platform_key", lambda: ("darwin", "arm64"))
    monkeypatch.setattr(
        tools, "install_libreoffice", lambda root: got.append(root) or root / "soffice"
    )
    assert main(["doctor", "--install", "libreoffice", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert got == [tmp_path / "data/tools"] and calls == []
    [row] = out["results"]
    assert row["check"] == "install libreoffice" and "LibreOffice 26.8.1, 285 MB" in row["detail"]


def test_install_archive_also_fetches_7zip(monkeypatch, tmp_path):
    calls: list[list[str]] = []
    _fake_install(monkeypatch, tmp_path, calls)
    got = []
    monkeypatch.setattr(tools, "platform_key", lambda: ("linux", "x86_64"))
    monkeypatch.setattr(tools, "install_seven_zip", lambda root: got.append(root) or root)
    assert main(["doctor", "--install", "archive"]) == 0
    assert calls[-1][-1].endswith("[archive]") and got == [tmp_path / "data/tools"]


def test_install_archive_keeps_the_venv_when_7zip_cant_download(monkeypatch, tmp_path, capsys):
    calls: list[list[str]] = []
    real = tools.install_seven_zip
    _fake_install(monkeypatch, tmp_path, calls)
    monkeypatch.setattr(tools, "platform_key", lambda: ("win32", "x86_64"))
    monkeypatch.setattr(tools, "install_seven_zip", real)
    assert main(["doctor", "--install", "archive", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    rows = {r["check"]: r for r in out["results"]}
    assert rows["install archive"]["ok"] is True
    assert rows["install 7zip"]["ok"] is False
    assert "no 7-Zip download for win32" in rows["install 7zip"]["detail"]
    assert "put 7zz or 7z on PATH" in rows["install 7zip"]["hint"]
    assert any(w.startswith("install 7zip:") for w in out["warnings"])
    assert (tmp_path / "data/venv/.meltify-version").read_text() == doctor.__version__


def _outside_launcher(monkeypatch, tmp_path, installer: str | None = None) -> list[list[str]]:
    import importlib.metadata
    import sys

    calls: list[list[str]] = []
    _fake_install(monkeypatch, tmp_path, calls)
    monkeypatch.delenv("MELTIFY_LAUNCHER")
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    monkeypatch.setattr(sys, "prefix", str(prefix))

    class Dist:
        def read_text(self, name):
            return installer

    monkeypatch.setattr(importlib.metadata, "distribution", lambda name: Dist())
    return calls


def test_install_outside_the_launcher_prints_the_command_instead(monkeypatch, tmp_path, capsys):
    calls = _outside_launcher(monkeypatch, tmp_path, "pip\n")
    monkeypatch.setattr(doctor, "installed_extras", lambda: [])
    assert main(["doctor", "--install", "office", "--json"]) == 3
    out = json.loads(capsys.readouterr().out)
    # A venv the launcher would use, but this meltify never would, isn't created
    assert calls == [] and not (tmp_path / "data/venv").exists()
    pip = f"{sys.executable} -m pip install 'meltify[office]=={__version__}'"
    assert out["errors"][0]["hint"] == pip
    assert out["summary"] == f"to add office, run {pip}"


def test_install_command_follows_how_meltify_was_installed(monkeypatch, tmp_path):
    import sys

    _outside_launcher(monkeypatch, tmp_path, "uv\n")
    # Extras already in place stay in the requirement, and the version stays pinned
    monkeypatch.setattr(doctor, "installed_extras", lambda: ["office"])
    spec = f"'meltify[media,office]=={__version__}'"
    assert doctor.extra_command("media") == (
        f"uv tool install {spec}, or {sys.executable} -m pip install {spec}"
    )
    (tmp_path / "prefix/uv-receipt.toml").write_text("")
    assert doctor.extra_command("media") == f"uv tool install {spec}"
    # Running from the launcher's venv counts even without the variable
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "data/venv"))
    assert doctor.launched({"CLAUDE_PLUGIN_DATA": str(tmp_path / "data")})


def test_install_archive_outside_the_launcher_still_fetches_7zip(monkeypatch, tmp_path, capsys):
    calls = _outside_launcher(monkeypatch, tmp_path)
    monkeypatch.setattr(doctor, "installed_extras", lambda: [])
    got = []
    monkeypatch.setattr(tools, "install_seven_zip", lambda root: got.append(root) or root)
    assert main(["doctor", "--install", "archive", "--json"]) == 3
    out = json.loads(capsys.readouterr().out)
    assert calls == [] and got == [tmp_path / "data/tools"]
    assert [r["check"] for r in out["results"]] == ["install 7zip"]
    assert f"pip install 'meltify[archive]=={__version__}'" in out["errors"][0]["hint"]


def test_unpinned_platforms_fail_with_a_hint(monkeypatch, tmp_path):
    monkeypatch.setattr(tools, "platform_key", lambda: ("win32", "x86_64"))
    with pytest.raises(RuntimeError, match="no LibreOffice download for win32"):
        doctor.install_tool("libreoffice", {"CLAUDE_PLUGIN_DATA": str(tmp_path)})


def _tar(path, files: dict[str, bytes], mode="w:xz"):
    import io
    import tarfile

    with tarfile.open(path, mode) as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


def _pin(path):
    import hashlib

    data = path.read_bytes()
    return tools.Download(
        f"https://example.test/{path.name}", hashlib.sha256(data).hexdigest(), len(data)
    )


def test_fetch_refuses_a_file_that_misses_its_pin(monkeypatch, tmp_path):
    import httpx

    body = b"release bytes"
    src = tmp_path / "a.tar.xz"
    src.write_bytes(body)
    good = _pin(src)

    def stream(method, url, **kw):
        return httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body))
        ).stream(method, url)

    monkeypatch.setattr(httpx, "stream", stream)
    got = FETCH(good, tmp_path / "dl")
    assert got.read_bytes() == body
    bad = tools.Download(good.url, "0" * 64, good.size)
    with pytest.raises(RuntimeError, match="pinned hash"):
        FETCH(bad, tmp_path / "dl2")
    assert not (tmp_path / "dl2" / "a.tar.xz").exists()


def test_fetch_stops_a_download_past_its_pinned_size(monkeypatch, tmp_path):
    import httpx

    sent = []

    def endless():
        # A hostile mirror that never ends, cut off well before it fills the disk
        for _ in range(64):
            sent.append(1)
            yield b"x" * (1 << 20)

    def stream(method, url, **kw):
        return httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=endless()))
        ).stream(method, url)

    monkeypatch.setattr(httpx, "stream", stream)
    pin = tools.Download("https://example.test/a.tar.xz", "0" * 64, 3 << 20)
    with pytest.raises(RuntimeError, match="larger than its pinned"):
        FETCH(pin, tmp_path / "dl")
    assert len(sent) <= 5
    assert not (tmp_path / "dl" / "a.tar.xz").exists()


def test_fetch_retries_a_stalled_mirror(monkeypatch, tmp_path):
    import httpx

    tries = []

    def get(url, target, limit):
        tries.append(url)
        raise httpx.ReadTimeout("stalled")

    monkeypatch.setattr(tools, "_get", get)
    with pytest.raises(RuntimeError, match="couldn't download"):
        FETCH(tools.Download("https://example.test/a", "0" * 64, 1), tmp_path)
    assert len(tries) == tools.DOWNLOAD_ATTEMPTS


def test_seven_zip_unpacks_only_the_binary_and_license(monkeypatch, tmp_path):
    release = _tar(
        tmp_path / "7z.tar.xz",
        {"7zz": b"\x7fELF", "License.txt": b"GNU LGPL", "../evil": b"x", "MANUAL/a.htm": b"m"},
    )
    monkeypatch.setattr(tools, "pinned", lambda table, what: _pin(release))
    monkeypatch.setattr(tools, "fetch", lambda item, folder: release)
    binary = tools.install_seven_zip(tmp_path / "tools")
    assert binary == tmp_path / "tools/7zip/7zz" and binary.stat().st_mode & 0o111
    assert sorted(p.name for p in binary.parent.iterdir()) == ["7zz", "License.txt"]
    assert not (tmp_path / "evil").exists()
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    assert tools.seven_zip() == str(binary)


def _deb(files: dict[str, bytes]) -> bytes:
    import io
    import tarfile

    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:xz") as tar:
        for name, body in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(body))
    members = [
        (b"debian-binary", b"2.0\n"),
        (b"control.tar.xz", b"c"),
        (b"data.tar.xz", data.getvalue()),
    ]
    out = b"!<arch>\n"
    for name, body in members:
        out += name.ljust(16) + b"0".ljust(12) + b"0".ljust(6) * 2 + b"100644".ljust(8)
        out += str(len(body)).encode().ljust(10) + b"`\n" + body + (b"\n" if len(body) % 2 else b"")
    return out


def test_linux_libreoffice_comes_out_of_the_official_debs(monkeypatch, tmp_path):
    import io
    import tarfile

    tarball = tmp_path / "lo_deb.tar.gz"
    with tarfile.open(tarball, "w:gz") as tar:
        for name, files in {
            "LO/DEBS/core.deb": {"./opt/libreoffice26.8/program/soffice": b"#!/bin/sh\n"},
            "LO/DEBS/menus.deb": {"./usr/local/bin/libreoffice": b"x", "/etc/evil": b"x"},
        }.items():
            body = _deb(files)
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    monkeypatch.setattr(
        tools, "pinned", lambda table, what: tools.Download("https://x/lo_deb.tar.gz", "", 0)
    )
    monkeypatch.setattr(tools, "fetch", lambda item, folder: tarball)
    soffice = tools.install_libreoffice(tmp_path / "tools")
    assert soffice == tmp_path / "tools/libreoffice/program/soffice" and soffice.is_file()
    # Only the install tree lands, nothing for /usr or /etc
    assert sorted(p.name for p in (tmp_path / "tools").iterdir()) == ["libreoffice"]


def test_install_archive_reports_a_7zip_download_that_answers_404(monkeypatch, tmp_path, capsys):
    import httpx

    calls: list[list[str]] = []
    real = tools.install_seven_zip
    _fake_install(monkeypatch, tmp_path, calls)
    monkeypatch.setattr(tools, "platform_key", lambda: ("linux", "x86_64"))
    monkeypatch.setattr(tools, "install_seven_zip", real)
    monkeypatch.setattr(tools, "fetch", FETCH)

    def stream(method, url, **kw):
        return httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(404, content=b"gone"))
        ).stream(method, url)

    monkeypatch.setattr(httpx, "stream", stream)
    assert main(["doctor", "--install", "archive", "--json"]) == 0
    rows = {r["check"]: r for r in json.loads(capsys.readouterr().out)["results"]}
    assert rows["install archive"]["ok"] is True
    assert rows["install 7zip"]["ok"] is False
    assert "404" in rows["install 7zip"]["detail"]
    assert (tmp_path / "data/venv/.meltify-version").read_text() == doctor.__version__
    assert not list((tmp_path / "data/tools").glob("**/*.tar.xz"))
