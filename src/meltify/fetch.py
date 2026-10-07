"""Download a URL safely so `read` can melt it like a local file

Media URLs go to `media`, while pages and documents are saved under a per-URL cache
folder. Every hop is checked against private addresses, and the connection is pinned to
the address that passed the check, so DNS rebinding can't swap it afterwards
"""

from __future__ import annotations

import hashlib
import importlib.util
import ipaddress
import json
import mimetypes
import re
import socket
import threading
import time
import urllib.robotparser
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import httpcore
import httpx

from meltify import __version__
from meltify.files import SEVEN_ZIP_MAGIC, safe_name

USER_AGENT = f"meltify/{__version__} (+https://github.com/jeon-jihyeon/meltify)"
MAX_REDIRECTS = 5
CONNECT_TIMEOUT = 10.0
TOTAL_TIMEOUT = 30.0
HOST_GAP = 1.0
ROBOTS_MAX = 500_000  # RFC 9309 lets crawlers stop reading after 500 KiB
RENDER_HINT = "meltify doctor --install render"

# Used only when yt-dlp isn't installed, so common video links still reach `media`
MEDIA_HOSTS = (
    "youtube.com",
    "youtu.be",
    "vimeo.com",
    "dailymotion.com",
    "twitch.tv",
    "tiktok.com",
    "soundcloud.com",
    "tv.naver.com",
    "chzzk.naver.com",
    "tv.kakao.com",
)
HTML_TYPES = {"text/html", "application/xhtml+xml"}
# Content types too vague to beat the suffix in the URL
GENERIC_TYPES = {"", "application/octet-stream", "binary/octet-stream", "text/plain"}
MAGIC = (
    (b"%PDF-", ".pdf"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (SEVEN_ZIP_MAGIC, ".7z"),
    (b"SQLite format 3\x00", ".sqlite"),
    (b"{\\rtf", ".rtf"),
)
ZIP_SUFFIXES = {".docx", ".xlsx", ".xlsm", ".pptx", ".epub", ".hwpx", ".odt", ".ods", ".odp"}
ZIP_SUFFIXES |= {".zip", ".jar", ".pages", ".numbers", ".key", ".ipynb"}
# Macro, template and slideshow variants, which a plain .zip name would unpack as an archive
ZIP_SUFFIXES |= {".docm", ".dotx", ".dotm", ".pptm", ".potx", ".potm", ".ppsx", ".ppsm"}
ZIP_SUFFIXES |= {".xltx", ".xltm", ".xlsb", ".show", ".cell", ".cbz", ".xps", ".oxps"}
ZIP_SUFFIXES |= {".odg", ".ott", ".ots", ".otp", ".otg"}


@dataclass
class FetchOptions:
    max_bytes: int = 20_000_000
    allow_private: bool = False
    ignore_robots: bool = False
    refresh: bool = False
    render: bool = False


@dataclass
class Fetched:
    url: str
    final_url: str
    kind: str  # "media" | "html" | "file"
    path: Path | None
    content_type: str
    fetched_at: str
    sha256: str
    etag: str | None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        # Tunnels and mapped forms carry an IPv4 address that decides where packets go
        inner = ip.ipv4_mapped or ip.sixtofour or (ip.teredo[1] if ip.teredo else None)
        if inner is not None:
            return inner.is_global
    return ip.is_global


def _resolve(host: str, port: int, allow_private: bool) -> list[str]:
    """Addresses for the host, refusing the whole host if any of them isn't public"""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ValueError(f"can't resolve {host}: {e}") from None
    ips = []
    for info in infos:
        ip = ipaddress.ip_address(str(info[4][0]).split("%")[0])
        if not allow_private and not _public(ip):
            raise ValueError(f"blocked {host}: {ip} isn't a public address (try --allow-private)")
        ips.append(str(ip))
    if not ips:
        raise ValueError(f"can't resolve {host}")
    return ips


def _check(url: httpx.URL, allow_private: bool) -> None:
    if url.scheme not in ("http", "https"):
        raise ValueError(f"only http and https URLs can be fetched, not {url.scheme or 'none'}")
    if not url.host:
        raise ValueError(f"no host in {url}")
    _resolve(url.host, url.port or (443 if url.scheme == "https" else 80), allow_private)


def check_url(url: str, allow_private: bool) -> None:
    """Raise ValueError unless the URL is http(s) and every address it resolves to is allowed"""
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL as e:
        raise ValueError(f"bad URL {url}: {e}") from None
    _check(parsed, allow_private)


class _PinnedBackend(httpcore.SyncBackend):
    # Connect to an address that passed the check, so a second DNS answer can't redirect us
    def __init__(self, allow_private: bool) -> None:
        self.allow_private = allow_private

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        ip = _resolve(host, port, self.allow_private)[0]
        return super().connect_tcp(ip, port, timeout, local_address, socket_options)


def _client(allow_private: bool) -> httpx.Client:
    transport = httpx.HTTPTransport()
    pool = getattr(transport, "_pool", None)
    # Without the pin a request would already be on its way before the peer check, so an httpx
    # release that moves this attribute has to fail loudly instead of fetching unguarded
    if pool is None or not hasattr(pool, "_network_backend"):
        raise RuntimeError("this httpx version can't pin addresses, so meltify won't fetch URLs")
    pool._network_backend = _PinnedBackend(allow_private)
    # Proxies from the environment would make the peer check see the proxy, not the site
    return httpx.Client(
        transport=transport,
        trust_env=False,
        follow_redirects=False,
        headers={"User-Agent": USER_AGENT},
        timeout=httpx.Timeout(CONNECT_TIMEOUT, connect=CONNECT_TIMEOUT),
    )


def _peer_ok(response: httpx.Response, allow_private: bool) -> None:
    stream = response.extensions.get("network_stream")
    addr = stream.get_extra_info("server_addr") if stream is not None else None
    if allow_private or not addr:
        return
    ip = ipaddress.ip_address(str(addr[0]).split("%")[0])
    if not _public(ip):
        raise ValueError(f"blocked {response.url.host}: connected to non-public {ip}")


_gap_lock = threading.Lock()
_last_hit: dict[str, float] = {}


def _wait_turn(host: str) -> None:
    # Be polite to one site even when `read` fetches many of its pages in parallel
    with _gap_lock:
        now = time.monotonic()
        start = max(now, _last_hit.get(host, -HOST_GAP) + HOST_GAP)
        _last_hit[host] = start
    if start > now:
        time.sleep(start - now)


_robots_lock = threading.Lock()
_robots: dict[str, urllib.robotparser.RobotFileParser] = {}


def _robots_for(client: httpx.Client, url: httpx.URL, opts: FetchOptions, deadline: float):
    origin = f"{url.scheme}://{url.netloc.decode('ascii')}"
    with _robots_lock:
        if origin in _robots:
            return _robots[origin]
    parser = urllib.robotparser.RobotFileParser(origin + "/robots.txt")
    try:
        status, body = _get_small(client, httpx.URL(origin + "/robots.txt"), opts, deadline)
    except httpx.HTTPError:
        # The page request will report the real network problem
        status, body = 404, b""
    if status >= 500:
        # RFC 9309: a server error on robots.txt means the whole site is off limits,
        # while any 4xx means there are no rules
        parser.disallow_all = True
    elif status >= 400:
        parser.allow_all = True
    else:
        parser.parse(body.decode("utf-8", "replace").splitlines())
    with _robots_lock:
        _robots[origin] = parser
    return parser


def _get_small(client: httpx.Client, url: httpx.URL, opts: FetchOptions, deadline: float):
    for _ in range(MAX_REDIRECTS + 1):
        _check(url, opts.allow_private)
        with client.stream("GET", url) as r:
            _peer_ok(r, opts.allow_private)
            if r.is_redirect and "location" in r.headers:
                url = r.url.join(r.headers["location"])
                continue
            body = bytearray()
            for chunk in r.iter_bytes():
                body += chunk
                if len(body) > ROBOTS_MAX or time.monotonic() > deadline:
                    break
            return r.status_code, bytes(body[:ROBOTS_MAX])
    return 404, b""


def is_media(url: str) -> bool:
    """True when yt-dlp has a site extractor for the URL, checked without any network"""
    if importlib.util.find_spec("yt_dlp") is not None:
        from yt_dlp.extractor import gen_extractor_classes

        return any(ie.suitable(url) and ie.ie_key() != "Generic" for ie in gen_extractor_classes())
    host = (httpx.URL(url).host or "").lower()
    return any(host == h or host.endswith("." + h) for h in MEDIA_HOSTS)


def _cache_dir(url: str, out_dir: Path) -> Path:
    return out_dir / hashlib.sha256(url.encode()).hexdigest()[:16]


def _stem(url: httpx.URL) -> str:
    name = PurePosixPath(url.path).name
    stem = safe_name(PurePosixPath(name).stem)[:60].strip("._")
    return stem or "index"


def _url_suffix(url: httpx.URL) -> str:
    suffix = PurePosixPath(url.path).suffix.lower()
    return suffix if re.fullmatch(r"\.[a-z0-9]{1,6}", suffix) else ""


def classify(
    content_type: str, head: bytes, url: httpx.URL, disposition: str = ""
) -> tuple[str, str]:
    """Kind and file suffix from the declared type, the first bytes and the names"""
    ctype = content_type.split(";")[0].strip().lower()
    lead = head[:1024].lstrip().lower()
    for magic, suffix in MAGIC:
        if head.startswith(magic):
            return "file", suffix
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "file", ".webp"
    named = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", disposition, re.I)
    named_suffix = PurePosixPath(named.group(1)).suffix.lower() if named else ""
    if head.startswith(b"PK\x03\x04"):
        for suffix in (named_suffix, _url_suffix(url), mimetypes.guess_extension(ctype) or ""):
            if suffix in ZIP_SUFFIXES:
                return "file", suffix
        return "file", ".zip"
    if ctype in HTML_TYPES or (
        ctype in GENERIC_TYPES and lead.startswith((b"<!doctype html", b"<html"))
    ):
        return "html", ".html"
    if named_suffix:
        return "file", named_suffix
    if ctype in GENERIC_TYPES and _url_suffix(url):
        return "file", _url_suffix(url)
    guessed = mimetypes.guess_extension(ctype) if ctype else None
    return "file", guessed or _url_suffix(url) or ".bin"


def _cached(folder: Path) -> dict | None:
    try:
        meta = json.loads((folder / "headers.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return meta if (folder / meta.get("name", "")).is_file() else None


def _result(url: str, folder: Path, meta: dict) -> Fetched:
    name = meta.get("rendered") or meta["name"]
    return Fetched(
        url=url,
        final_url=meta["final_url"],
        kind=meta["kind"],
        path=folder / name,
        content_type=meta["content_type"],
        fetched_at=meta["fetched_at"],
        sha256=meta["sha256"],
        etag=meta.get("etag"),
    )


def fetch(url: str, out_dir: Path, opts: FetchOptions) -> Fetched:
    """Download one URL into `out_dir`, or hand a video URL back as kind media

    Raises ValueError for blocked, unreachable or oversized URLs
    """
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL as e:
        raise ValueError(f"bad URL {url}: {e}") from None
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"only http and https URLs can be fetched, not {parsed.scheme or 'none'}")
    if opts.render and importlib.util.find_spec("playwright") is None:
        raise ValueError(f"--render needs Playwright, install it with `{RENDER_HINT}`")
    if is_media(url):
        return Fetched(url, url, "media", None, "", _now(), "", None)

    folder = _cache_dir(url, out_dir)
    cached = None if opts.refresh else _cached(folder)
    deadline = time.monotonic() + TOTAL_TIMEOUT
    try:
        with _client(opts.allow_private) as client:
            meta = _download(client, parsed, folder, cached, opts, deadline)
            if opts.render and meta["kind"] == "html" and not meta.get("rendered"):
                meta["rendered"] = _render(meta["final_url"], folder, opts)
                _save_meta(folder, meta)
    except httpx.TimeoutException:
        raise ValueError(f"timed out fetching {url}") from None
    except httpx.HTTPError as e:
        raise ValueError(f"couldn't fetch {url}: {type(e).__name__}: {e}") from None
    return _result(url, folder, meta)


def _save_meta(folder: Path, meta: dict) -> None:
    (folder / "headers.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")


def _download(
    client: httpx.Client,
    url: httpx.URL,
    folder: Path,
    cached: dict | None,
    opts: FetchOptions,
    deadline: float,
) -> dict:
    for _ in range(MAX_REDIRECTS + 1):
        _check(url, opts.allow_private)
        if not opts.ignore_robots:
            robots = _robots_for(client, url, opts, deadline)
            if not robots.can_fetch("meltify", str(url)):
                raise ValueError(f"robots.txt disallows {url} (try --ignore-robots)")
        headers = {}
        if cached:
            if cached.get("etag"):
                headers["If-None-Match"] = cached["etag"]
            if cached.get("last_modified"):
                headers["If-Modified-Since"] = cached["last_modified"]
        _wait_turn(url.host)
        if time.monotonic() > deadline:
            raise ValueError(f"timed out fetching {url}")
        with client.stream("GET", url, headers=headers) as r:
            _peer_ok(r, opts.allow_private)
            if r.is_redirect and "location" in r.headers:
                url = r.url.join(r.headers["location"])
                continue
            if r.status_code == 304 and cached:
                return cached
            if r.status_code >= 400:
                raise ValueError(f"HTTP {r.status_code} from {url}")
            return _store(r, url, folder, opts.max_bytes, deadline)
    raise ValueError(f"more than {MAX_REDIRECTS} redirects")


def _store(r: httpx.Response, url: httpx.URL, folder: Path, max_bytes: int, deadline: float):
    declared = r.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > max_bytes:
        raise ValueError(f"{url} is {int(declared):,} bytes, over the {max_bytes:,} byte limit")
    folder.mkdir(parents=True, exist_ok=True)
    part = folder / "download.part"
    digest = hashlib.sha256()
    size = 0
    head = b""
    try:
        with part.open("wb") as f:
            for chunk in r.iter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError(f"{url} is over the {max_bytes:,} byte limit")
                if time.monotonic() > deadline:
                    raise ValueError(f"timed out fetching {url}")
                if len(head) < 1024:
                    head += chunk[: 1024 - len(head)]
                digest.update(chunk)
                f.write(chunk)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    ctype = r.headers.get("content-type", "")
    kind, suffix = classify(ctype, head, url, r.headers.get("content-disposition", ""))
    # Clear out the earlier fetch, whose copy may carry another suffix and whose render no
    # longer matches
    for old in folder.iterdir():
        if old != part:
            old.unlink()
    name = _stem(url) + suffix
    part.rename(folder / name)
    meta = {
        "final_url": str(url),
        "status": r.status_code,
        "kind": kind,
        "name": name,
        "content_type": ctype,
        "etag": r.headers.get("etag"),
        "last_modified": r.headers.get("last-modified"),
        "fetched_at": _now(),
        "sha256": digest.hexdigest(),
        "bytes": size,
    }
    _save_meta(folder, meta)
    return meta


def _render(url: str, folder: Path, opts: FetchOptions) -> str:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    def guard(route) -> None:
        # The browser loads scripts and XHRs on its own, so give each one the same check
        try:
            _check(httpx.URL(route.request.url), opts.allow_private)
        except ValueError:
            route.abort()
            return
        route.continue_()

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except PlaywrightError:
            try:
                browser = p.chromium.launch(headless=True)
            except PlaywrightError as e:
                raise ValueError(f"no browser for --render, run `{RENDER_HINT}`: {e}") from None
        try:
            page = browser.new_page(user_agent=USER_AGENT)
            page.route("**/*", guard)
            page.goto(url, wait_until="networkidle", timeout=TOTAL_TIMEOUT * 1000)
            html = page.content()
        except PlaywrightError as e:
            raise ValueError(f"rendering {url} failed: {e}") from None
        finally:
            browser.close()
    name = "rendered.html"
    (folder / name).write_text(html, encoding="utf-8")
    return name
