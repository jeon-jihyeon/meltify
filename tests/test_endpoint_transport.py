import ipaddress
import json
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import httpcore
import httpx
import pytest

from meltify import fetch, llm
from meltify.engines import asr, ocr


@pytest.fixture
def server():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def respond(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            received.append((self.path, self.headers.get("Authorization"), body))
            data = {"choices": [{"message": {"content": "acme"}}], "text": "acme"}
            if self.path.startswith("/malformed"):
                data = {"segments": [{"start": "acme-response-C1", "end": 1, "text": "acme"}]}
            payload = json.dumps(data).encode()
            if self.path.startswith("/reflect"):
                self.send_response(
                    503, "acme " + self.headers.get("Authorization", "") + body.decode("utf-8")
                )
            else:
                self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = respond

        def log_message(self, *args):
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{http.server_port}/v1", received
    finally:
        http.shutdown()
        http.server_close()
        thread.join(5)


@pytest.mark.parametrize("kind", ["ocr", "asr", "probe"])
def test_endpoint_bypasses_environment_proxy(server, tmp_path, monkeypatch, kind):
    url, received = server
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(key, "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "missing-ca.pem"))
    monkeypatch.setenv("ACME_TOKEN", "acme")
    endpoint = llm.Endpoint("served", url, "acme", key_env="ACME_TOKEN", timeout=2)
    source = tmp_path / "input"
    source.write_bytes(b"acme")
    if kind == "ocr":
        engine = ocr.Remote("served", "acme", "ACME_TOKEN", url, endpoint)
        assert [box.text for box in engine.recognize(source, (1, 1))] == ["acme"]
    elif kind == "asr":
        engine = asr.Api("acme", "ACME_TOKEN", url, "served", endpoint)
        assert [segment.text for segment in engine.transcribe(source, "en")] == ["acme"]
    else:
        assert llm.answers(url, "acme") is None
    assert len(received) == 1
    assert received[0][1] == "Bearer acme"


@pytest.mark.parametrize("kind", ["post", "probe"])
def test_new_public_dns_answer_is_refused_before_connecting(monkeypatch, kind):
    answers = iter(([ipaddress.ip_address("127.0.0.1")], [ipaddress.ip_address("192.0.2.1")]))
    monkeypatch.setattr(fetch, "_addresses", lambda host, port: next(answers))
    # The documentation address stands in for a public answer without dialing the Internet
    monkeypatch.setattr(fetch, "_public", lambda ip: str(ip) == "192.0.2.1")
    endpoint = llm.Endpoint("served", "http://acme.example.com/v1", "acme")
    assert endpoint.missing() is None

    def unexpected(*args, **kwargs):
        pytest.fail("endpoint tried to connect before checking the new DNS answer")

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", unexpected)
    if kind == "post":
        with pytest.raises(ValueError, match="at connection time"):
            llm.post(endpoint.base_url + "/chat/completions", private=True, json={})
    else:
        assert llm.answers(endpoint.base_url) == (
            "endpoint needs loopback or private addresses at connection time"
        )


@pytest.mark.parametrize("case", ["header", "unicode_header", "reason"])
@pytest.mark.parametrize("debug", [False, True])
def test_endpoint_errors_never_echo_credentials_or_request_body(
    server, tmp_path, monkeypatch, capsys, case, debug
):
    from PIL import Image

    from meltify import cli

    url, _ = server
    if case == "reason":
        url = url.replace("/v1", "/reflect/v1")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    token = {"header": "acme-C1\n", "unicode_header": "acme-C1\u00e9"}.get(case, "acme-C1")
    monkeypatch.setenv("ACME_TOKEN", token)
    monkeypatch.setenv("MELTIFY_DEBUG", "1" if debug else "")
    project = tmp_path / "config.toml"
    project.write_text(
        "[ocr.endpoints.served]\n"
        f'base_url = "{url}"\n'
        'model = "acme"\nkey_env = "ACME_TOKEN"\nprompt = "acme-request-C1"\n'
    )
    image = tmp_path / "input.png"
    Image.new("RGB", (64, 64), "white").save(image)
    assert (
        cli.main(
            [
                "read",
                str(image),
                "--config",
                str(project),
                "--engines",
                "served",
                "--refresh",
                "--upscale",
                "1",
                "--out",
                str(tmp_path / "out"),
                "--json",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] and not payload["complete"]
    expected = {"header": "LocalProtocolError", "unicode_header": "UnicodeEncodeError"}.get(
        case, "HTTP 503"
    )
    assert any(expected in message for message in payload["warnings"])
    for text in (captured.out, captured.err):
        assert "acme-C1" not in text
        assert "acme-request-C1" not in text
        assert "Bearer" not in text
        assert "\\xe9" not in text


@pytest.mark.parametrize("case", ["header", "reason"])
@pytest.mark.parametrize("debug", [False, True])
def test_doctor_probe_never_echoes_credentials(server, tmp_path, monkeypatch, capsys, case, debug):
    from meltify import cli

    url, _ = server
    if case == "reason":
        url = url.replace("/v1", "/reflect/v1")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("ACME_TOKEN", "acme-C1\n" if case == "header" else "acme-C1")
    monkeypatch.delenv("ACME_UNUSED", raising=False)
    monkeypatch.setenv("MELTIFY_DEBUG", "1" if debug else "")
    monkeypatch.setenv("NO_PROXY", "*")
    project = tmp_path / "doctor.toml"
    project.write_text(
        '[llm]\nopenai_key_env = "ACME_TOKEN"\n'
        'anthropic_key_env = "ACME_UNUSED"\ngemini_key_env = "ACME_UNUSED"\n'
        f'openai_base_url = "{url}"\n'
    )
    assert cli.main(["doctor", "--quick", "--probe", "--config", str(project), "--json"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    [row] = [r for r in payload["results"] if r["check"].startswith("probe ")]
    assert row["check"] == "probe openai" and not row["ok"]
    assert ("LocalProtocolError" if case == "header" else "http 503") in row["detail"]
    for text in (captured.out, captured.err):
        assert "acme-C1" not in text
        assert "Bearer" not in text


@pytest.mark.parametrize("case", ["userinfo", "query", "public"])
def test_doctor_endpoint_hint_never_echoes_url_credentials(
    server, tmp_path, monkeypatch, capsys, case
):
    from meltify import cli

    url, _ = server
    url = url.replace("/v1", "/reflect/v1")
    if case == "userinfo":
        url = url.replace("http://", "http://ann:acme-C1@")
    elif case == "query":
        url += "?token=acme-C1"
    else:
        url = "http://ann:acme-C1@192.0.2.1/v1?token=acme-C1"
        monkeypatch.setattr(fetch, "private_only", lambda _: False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    project = tmp_path / "doctor.toml"
    project.write_text(f'[ocr.endpoints.served]\nbase_url = "{url}"\nmodel = "acme"\n')
    assert cli.main(["doctor", "--config", str(project), "--json"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    row = next(r for r in payload["results"] if r["check"] == "ocr served")
    assert not row["ok"] and row["hint"]
    for text in (captured.out, captured.err):
        assert "acme-C1" not in text
        assert url not in text


def test_invalid_speech_response_has_no_raw_value_or_exception_chain(server, tmp_path):
    url, _ = server
    url = url.replace("/v1", "/malformed/v1")
    endpoint = llm.Endpoint("served", url, "acme")
    audio = tmp_path / "input.wav"
    audio.write_bytes(b"acme")
    engine = asr.Api("acme", "", url, "served", endpoint)
    with pytest.raises(ValueError, match="invalid transcription response") as error:
        engine.transcribe(audio, "en")
    assert "acme-response-C1" not in "".join(traceback.format_exception(error.value))


def test_invalid_endpoint_url_has_no_raw_value_or_exception_chain():
    url = "http://127.0.0.1:acme-C1/v1"
    with pytest.raises(httpx.HTTPError, match="InvalidURL") as error:
        llm.post(url, json={})
    assert "acme-C1" not in "".join(traceback.format_exception(error.value))
    assert llm.answers(url) == "model server not answering: InvalidURL"


@pytest.mark.parametrize("provider", ["anthropic", "gemini", "openai"])
def test_invalid_vision_response_has_no_raw_exception_chain(tmp_path, monkeypatch, provider):
    def invalid(*args, **kwargs):
        return httpx.Response(200, json="acme-response-C1")

    monkeypatch.setattr(llm, "post", invalid)
    image = tmp_path / "input.png"
    image.write_bytes(b"acme")
    with pytest.raises(ValueError, match="invalid transcription response") as error:
        if provider == "anthropic":
            llm.anthropic_vision(image, "acme", "acme")
        elif provider == "gemini":
            llm.gemini_vision(image, "acme", "acme")
        else:
            llm.openai_vision(image, "acme", "acme", "http://example.com/v1")
    assert "acme-response-C1" not in "".join(traceback.format_exception(error.value))
