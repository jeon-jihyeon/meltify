from __future__ import annotations

import contextlib
import importlib.util
import ipaddress
import socket
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from meltify import fetch as fetch_mod
from meltify.fetch import FetchOptions, fetch

REAL_GETADDRINFO = socket.getaddrinfo
PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
HTML = b"<!doctype html><html><head><title>t</title></head><body><p>hello</p></body></html>"


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only literal addresses and a few test names resolve, so nothing reaches real DNS
    names = {"localtest.me": "127.0.0.1"}

    def getaddrinfo(host, port, *args, **kwargs):
        host = names.get(host, host)
        return REAL_GETADDRINFO(host, port, type=socket.SOCK_STREAM, flags=socket.AI_NUMERICHOST)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(fetch_mod, "HOST_GAP", 0.0)


class Site:
    def __init__(self) -> None:
        self.hits: list[tuple[str, dict[str, str]]] = []
        self.robots = "User-agent: *\nDisallow: /private\n"
        site = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                site.hits.append((self.path, dict(self.headers)))
                name = self.path.split("?")[0].strip("/").split("/")[0]
                route = getattr(site, "r_" + name.replace(".", "_"))
                # The client hangs up early in the limit and timeout tests
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    route(self)

            def log_message(self, *args: object) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, args=(0.05,), daemon=True).start()

    @staticmethod
    def send(h, body: bytes, ctype: str | None, status: int = 200, **headers: str) -> None:
        h.send_response(status)
        if ctype:
            h.send_header("Content-Type", ctype)
        for k, v in headers.items():
            h.send_header(k.replace("_", "-"), v)
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    def r_robots_txt(self, h) -> None:
        self.send(h, self.robots.encode(), "text/plain")

    def r_page(self, h) -> None:
        self.send(h, HTML, "text/html; charset=utf-8")

    def r_paper(self, h) -> None:
        self.send(h, PDF, "application/octet-stream")

    def r_logo(self, h) -> None:
        self.send(h, PNG, "image/png")

    def r_report_csv(self, h) -> None:
        self.send(h, b"a,b\n1,2\n", "text/plain")

    def r_download(self, h) -> None:
        body = b"PK\x03\x04" + b"\x00" * 40
        self.send(
            h,
            body,
            "application/octet-stream",
            Content_Disposition='attachment; filename="q3.xlsx"',
        )

    def r_private(self, h) -> None:
        self.send(h, HTML, "text/html")

    def r_big(self, h) -> None:
        # No Content-Length, so the limit has to trip while streaming
        h.send_response(200)
        h.send_header("Content-Type", "text/plain")
        h.end_headers()
        for _ in range(50):
            h.wfile.write(b"x" * 1000)

    def r_declared(self, h) -> None:
        self.send(h, b"y" * 5000, "text/plain")

    def r_slow(self, h) -> None:
        time.sleep(1.5)
        self.send(h, HTML, "text/html")

    def r_cached(self, h) -> None:
        if h.headers.get("If-None-Match") == '"v1"':
            self.send(h, b"", None, status=304, ETag='"v1"')
        else:
            self.send(h, HTML, "text/html", ETag='"v1"')

    def r_hop(self, h) -> None:
        target = h.path.split("to=", 1)[1]
        self.send(h, b"", "text/plain", status=302, Location=target)

    def r_loop(self, h) -> None:
        self.send(h, b"", "text/plain", status=302, Location="/loop")

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def site() -> Iterator[Site]:
    s = Site()
    yield s
    s.close()


LOCAL = FetchOptions(allow_private=True)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.org/a",
        "http://127.0.0.1/",
        "http://0x7f000001/",
        "http://2130706433/",
        "http://[::ffff:127.0.0.1]/",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.64.0.1/",
        "http://10.0.0.1/",
        "http://localtest.me/",
    ],
)
def test_unsafe_urls_are_blocked(url: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        fetch(url, tmp_path, FetchOptions())
    assert not any(tmp_path.iterdir())


def test_ipv6_wrappers_of_private_ipv4_are_not_public() -> None:
    for ip in ("::ffff:10.0.0.1", "2002:7f00:1::1", "2002:a9fe:a9fe::1"):
        assert not fetch_mod._public(ipaddress.ip_address(ip))
    assert fetch_mod._public(ipaddress.ip_address("8.8.8.8"))


def test_redirect_to_private_ip_is_blocked(
    site: Site, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pretend the test server is a public host, so only the second hop is private
    real = fetch_mod._public
    monkeypatch.setattr(fetch_mod, "_public", lambda ip: str(ip) == "127.0.0.1" or real(ip))
    with pytest.raises(ValueError, match="blocked 169.254.169.254"):
        fetch(f"{site.base}/hop?to=http://169.254.169.254/latest/", tmp_path, FetchOptions())
    assert [p for p, _ in site.hits if p.startswith("/hop")]


def test_dns_rebinding_after_the_check_is_caught(
    site: Site, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The first answer passes the check, the one used to connect points inside
    answers = iter(["93.184.216.34", "127.0.0.1"])

    def getaddrinfo(host, port, *args, **kwargs):
        ip = next(answers, "127.0.0.1") if host == "rebind.test" else host
        return REAL_GETADDRINFO(ip, port, type=socket.SOCK_STREAM, flags=socket.AI_NUMERICHOST)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    port = site.base.rsplit(":", 1)[1]
    opts = FetchOptions(ignore_robots=True)
    with pytest.raises(ValueError, match="blocked rebind.test: 127.0.0.1"):
        fetch(f"http://rebind.test:{port}/page", tmp_path, opts)
    assert not site.hits


@pytest.mark.parametrize(
    ("path", "kind", "suffix"),
    [
        ("/page", "html", ".html"),
        ("/paper", "file", ".pdf"),
        ("/logo", "file", ".png"),
        ("/report.csv", "file", ".csv"),
        ("/download", "file", ".xlsx"),
    ],
)
def test_content_type_and_magic_pick_the_kind(
    site: Site, tmp_path: Path, path: str, kind: str, suffix: str
) -> None:
    got = fetch(site.base + path, tmp_path, LOCAL)
    assert got.kind == kind
    assert got.path is not None and got.path.suffix == suffix
    assert got.final_url == site.base + path
    assert len(got.sha256) == 64 and got.fetched_at


def test_redirects_are_followed_within_the_limit(site: Site, tmp_path: Path) -> None:
    got = fetch(f"{site.base}/hop?to={site.base}/page", tmp_path, LOCAL)
    assert got.url.endswith("/hop?to=" + site.base + "/page")
    assert got.final_url == site.base + "/page"
    assert got.kind == "html"


def test_redirect_loops_stop_after_five_hops(site: Site, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="more than 5 redirects"):
        fetch(site.base + "/loop", tmp_path, LOCAL)
    assert len([p for p, _ in site.hits if p == "/loop"]) == 6


def test_streamed_body_over_the_limit_aborts(site: Site, tmp_path: Path) -> None:
    opts = FetchOptions(allow_private=True, max_bytes=10_000)
    with pytest.raises(ValueError, match="limit"):
        fetch(site.base + "/big", tmp_path, opts)
    assert not list(tmp_path.rglob("*.part"))


def test_declared_length_over_the_limit_aborts(site: Site, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="5,000 bytes"):
        fetch(site.base + "/declared", tmp_path, FetchOptions(allow_private=True, max_bytes=1000))


def test_slow_server_times_out(site: Site, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fetch_mod, "CONNECT_TIMEOUT", 0.3)
    monkeypatch.setattr(fetch_mod, "TOTAL_TIMEOUT", 0.5)
    with pytest.raises(ValueError, match="timed out"):
        fetch(site.base + "/slow", tmp_path, LOCAL)


def test_etag_turns_a_refetch_into_a_304(site: Site, tmp_path: Path) -> None:
    first = fetch(site.base + "/cached", tmp_path, LOCAL)
    second = fetch(site.base + "/cached", tmp_path, LOCAL)
    assert second.path == first.path and second.sha256 == first.sha256
    assert first.etag == '"v1"'
    sent = [h for p, h in site.hits if p == "/cached"]
    assert "If-None-Match" not in sent[0] and sent[1]["If-None-Match"] == '"v1"'
    fetch(site.base + "/cached", tmp_path, FetchOptions(allow_private=True, refresh=True))
    assert "If-None-Match" not in [h for p, h in site.hits if p == "/cached"][2]


def test_robots_disallow_is_obeyed_unless_ignored(site: Site, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="robots.txt"):
        fetch(site.base + "/private/x", tmp_path, LOCAL)
    assert not [p for p, _ in site.hits if p.startswith("/private")]
    opts = FetchOptions(allow_private=True, ignore_robots=True)
    assert fetch(site.base + "/private/x", tmp_path, opts).kind == "html"


def test_user_agent_names_meltify(site: Site, tmp_path: Path) -> None:
    fetch(site.base + "/page", tmp_path, LOCAL)
    assert all(h["User-Agent"].startswith("meltify/") for _, h in site.hits)


def test_same_host_requests_keep_a_gap(
    site: Site, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fetch_mod, "HOST_GAP", 0.4)
    fetch_mod._last_hit.clear()
    start = time.monotonic()
    fetch(site.base + "/page", tmp_path, LOCAL)
    fetch(site.base + "/logo", tmp_path, LOCAL)
    assert time.monotonic() - start >= 0.4


def test_video_urls_go_to_media_without_network(tmp_path: Path) -> None:
    got = fetch("https://www.youtube.com/watch?v=dQw4w9WgXcQ", tmp_path, FetchOptions())
    assert got.kind == "media" and got.path is None
    assert not any(tmp_path.iterdir())


@pytest.mark.skipif(importlib.util.find_spec("playwright") is not None, reason="render installed")
def test_render_without_playwright_names_the_install(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="meltify doctor --install render"):
        fetch("https://example.org/", tmp_path, FetchOptions(render=True))


def test_peer_address_is_checked_after_connect() -> None:
    class Stream:
        @staticmethod
        def get_extra_info(key: str):
            return ("10.0.0.5", 80) if key == "server_addr" else None

    request = httpx.Request("GET", "http://example.org/")
    r = httpx.Response(200, extensions={"network_stream": Stream()}, request=request)
    with pytest.raises(ValueError, match="non-public 10.0.0.5"):
        fetch_mod._peer_ok(r, allow_private=False)
    fetch_mod._peer_ok(r, allow_private=True)


def test_a_client_that_cant_pin_addresses_refuses_to_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    # The pin hooks into httpx internals, so a release that moves them must not fetch unguarded
    with fetch_mod._client(False) as client:
        pool = client._transport._pool  # type: ignore[attr-defined]
        assert isinstance(pool._network_backend, fetch_mod._PinnedBackend)
    monkeypatch.setattr(httpx.HTTPTransport, "__init__", lambda self, *a, **k: None)
    with pytest.raises(RuntimeError, match="can't pin addresses"):
        fetch_mod._client(False)
