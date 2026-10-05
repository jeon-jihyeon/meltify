from __future__ import annotations

import argparse
import importlib.util
import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Any

from meltify import __version__
from meltify.evidence import MISSING, Envelope

NAME = "doctor"
HELP = "check engines, binaries and API keys that the other commands need"
COLUMNS = ["check", "ok", "detail", "used_by", "hint"]

IS_MAC = sys.platform == "darwin"
IS_APPLE_SILICON = IS_MAC and platform.machine() == "arm64"

# Each entry: module, its users, install hint, and whether the base install needs it
MODULES = [
    ("pymupdf", "read ocr hidden", "pip install meltify", True),
    ("openpyxl", "read", "pip install meltify", True),
    ("PIL", "ocr media", "pip install meltify", True),
    ("numpy", "ocr media", "pip install meltify", True),
    ("jsonschema", "check", "pip install meltify", True),
    ("httpx", "submit ocr", "pip install meltify", True),
    ("trafilatura", "read urls", "pip install meltify", True),
    ("hwpx", "read hwp hwpx", "pip install meltify", True),
    ("striprtf", "read rtf", "pip install meltify", True),
    ("pi_heif", "read ocr heic avif", "pip install meltify", True),
    ("markitdown", "read docx pptx msg", "meltify doctor --install office", False),
    ("python_calamine", "read xls", "meltify doctor --install office", False),
    ("legacy_doc", "read doc", "meltify doctor --install office", False),
    ("libarchive", "read 7z rar", "meltify doctor --install archive", False),
    ("numbers_parser", "read numbers", "meltify doctor --install iwork", False),
    ("playwright", "read --render", "meltify doctor --install render", False),
    ("yt_dlp", "media urls", "meltify doctor --install media", False),
    ("paddleocr", "ocr engine paddle", "meltify doctor --install ocr-paddle", False),
]
if IS_MAC:
    MODULES.append(("ocrmac", "ocr engine vision", "pip install meltify", True))
if IS_APPLE_SILICON:
    MODULES.append(("mlx_whisper", "media read asr", "meltify doctor --install asr-mlx", False))

BINARIES = [
    ("ffmpeg", "media read audio", "brew install ffmpeg or apt install ffmpeg"),
    ("ffprobe", "media", "installed with ffmpeg"),
    ("deno", "media youtube", "brew install deno"),
    ("whisper-cli", "asr engine whispercpp", "brew install whisper-cpp"),
    ("uv", "launcher and --install", "https://docs.astral.sh/uv/"),
]
SOFFICE_HINT = "brew install --cask libreoffice or apt install libreoffice"
# Where Playwright's `channel="chrome"` looks first, so --render can skip its own Chromium
CHROME = (
    Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    Path("/opt/google/chrome/chrome"),
)
EXTRAS = ["office", "archive", "iwork", "render", "media", "asr-mlx", "ocr-paddle", "all"]


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--quick", action="store_true", help="skip binaries and optional modules")
    p.add_argument(
        "--install",
        choices=EXTRAS,
        help="install an optional extra into the launcher's venv in the data dir",
    )
    p.add_argument("--probe", action="store_true", help="make one tiny paid call per API key")


def _row(check: str, ok: bool, detail: str, used_by: str = "", hint: str = "") -> dict[str, Any]:
    return {
        "check": check,
        "ok": ok,
        "detail": detail,
        "used_by": used_by,
        "hint": "" if ok else hint,
    }


def checks(settings: dict[str, Any], quick: bool, env: dict[str, str]) -> list[dict[str, Any]]:
    rows = [
        _row(
            "python", sys.version_info >= (3, 11), platform.python_version(), "all", "Python 3.11+"
        ),
        _row("meltify", True, __version__),
    ]
    for module, used_by, hint, required in MODULES:
        if quick and not required:
            continue
        found = importlib.util.find_spec(module) is not None
        rows.append(
            _row(f"module {module}", found, "importable" if found else "missing", used_by, hint)
        )

    llm = settings.get("llm", {})
    for key in ("anthropic_key_env", "gemini_key_env", "openai_key_env"):
        name = llm.get(key, "")
        # Report only presence, since key values must never reach a transcript
        present = bool(name and env.get(name))
        rows.append(
            _row(
                f"key {name}",
                present,
                "set" if present else "unset",
                "ocr llm engines",
                f"export {name}=...",
            )
        )

    if not quick:
        for binary, used_by, hint in BINARIES:
            path = shutil.which(binary)
            rows.append(
                _row(f"bin {binary}", path is not None, path or "not on PATH", used_by, hint)
            )
        rows += system_parts()
        out = Path(settings.get("out_dir", "."))
        probe = out if out.exists() else Path.cwd()
        free = shutil.disk_usage(probe).free / 2**30
        rows.append(
            _row("disk free", free > 5, f"{free:.0f} GB at {probe}", "model downloads", "free 5 GB")
        )

    rows.append(_row("data dir", True, str(data_dir(env))))
    return rows


def chrome() -> str | None:
    found = shutil.which("google-chrome") or shutil.which("google-chrome-stable")
    return found or next((str(p) for p in CHROME if p.exists()), None)


def system_parts() -> list[dict[str, Any]]:
    """Programs and shared libraries outside the venv that some formats lean on"""
    from meltify.converters.archive import _libarchive_ready
    from meltify.converters.legacy import soffice

    binary = soffice()
    rows = [
        _row(
            "bin soffice",
            binary is not None,
            binary or "not found",
            "read ppt, doc fallback",
            SOFFICE_HINT,
        )
    ]
    # libarchive-c is only a binding, so the shared library has to be there as well
    if importlib.util.find_spec("libarchive") is not None:
        ready = _libarchive_ready()
        rows.append(
            _row(
                "lib libarchive",
                ready,
                "loaded" if ready else "binding installed, shared library missing",
                "read 7z rar",
                "brew install libarchive or apt install libarchive13",
            )
        )
    if importlib.util.find_spec("playwright") is not None:
        found = chrome()
        rows.append(
            _row(
                "browser chrome",
                found is not None,
                found or "not found, --render needs Playwright's Chromium",
                "read --render",
                "meltify doctor --install render",
            )
        )
    return rows


def engines(settings: dict[str, Any]) -> list[dict[str, Any]]:
    from meltify.engines import asr, ocr

    rows = []
    for name in ("vision", "paddle", "gemini", "claude", "openai"):
        why = ocr.build(name, settings).missing()
        rows.append(
            _row(
                f"ocr {name}",
                why is None,
                "ready" if why is None else "unavailable",
                "ocr",
                why or "",
            )
        )
    for engine in asr.engines(settings).values():
        why = engine.missing()
        rows.append(
            _row(
                f"asr {engine.name}",
                why is None,
                "ready" if why is None else "unavailable",
                "media",
                why or "",
            )
        )
    return rows


def data_dir(env: dict[str, str]) -> Path:
    # Must match the launcher, so it finds the venv installed here
    if env.get("CLAUDE_PLUGIN_DATA"):
        return Path(env["CLAUDE_PLUGIN_DATA"])
    base = env.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "meltify"


def source_checkout() -> Path | None:
    here = Path(__file__).resolve()
    for d in here.parents:
        p = d / "pyproject.toml"
        if p.is_file() and 'name = "meltify"' in p.read_text("utf-8"):
            return d
    return None


def install(extra: str, env: dict[str, str]) -> list[dict[str, Any]]:
    from meltify import safe

    uv = safe.require_binary("uv", "install uv from https://docs.astral.sh/uv/")
    venv = data_dir(env) / "venv"
    if not (venv / "bin" / "python").exists():
        safe.run([uv, "venv", "--quiet", "--python", "3.12", str(venv)])
    checkout = source_checkout()
    if checkout is not None:
        spec = f"{checkout}[{extra}]"
    elif env.get("MELTIFY_FROM_GIT") == "1":
        spec = f"meltify[{extra}] @ git+https://github.com/jeon-jihyeon/meltify@v{__version__}"
    else:
        spec = f"meltify[{extra}]=={__version__}"
    python = str(venv / "bin" / "python")
    safe.run([uv, "pip", "install", "--quiet", "--python", python, spec])
    rows = [_row(f"install {extra}", True, str(venv), "launcher", "")]
    if extra == "render":
        # --render drives the installed Chrome first, so only download Chromium without one
        found = chrome()
        if found is None:
            safe.run([python, "-m", "playwright", "install", "chromium"])
        rows.append(
            _row("browser chrome", True, found or "Playwright Chromium", "read --render", "")
        )
    (venv / ".meltify-version").write_text(__version__, encoding="utf-8")
    return rows


def probe(settings: dict[str, Any], env: dict[str, str]) -> list[dict[str, Any]]:
    """One tiny real call per configured key, costing a few tokens"""
    import time

    import httpx

    llm = settings["llm"]
    calls = {
        "anthropic": (
            llm["anthropic_key_env"],
            lambda k: httpx.post(
                "https://api.anthropic.com/v1/messages",
                json={
                    "model": llm["claude_model"],
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "ping"}],
                },
                headers={"x-api-key": k, "anthropic-version": "2023-06-01"},
                timeout=30,
            ),
        ),
        "gemini": (
            llm["gemini_key_env"],
            lambda k: httpx.get(
                "https://generativelanguage.googleapis.com/v1beta/models",
                headers={"x-goog-api-key": k},
                timeout=30,
            ),
        ),
        "openai": (
            llm["openai_key_env"],
            lambda k: httpx.get(
                f"{llm['openai_base_url'].rstrip('/')}/models",
                headers={"Authorization": f"Bearer {k}"},
                timeout=30,
            ),
        ),
    }
    rows = []
    for name, (key_env, call) in calls.items():
        if not env.get(key_env):
            continue
        start = time.monotonic()
        try:
            r = call(env[key_env])
            ok = r.status_code < 400
            detail = f"http {r.status_code} in {time.monotonic() - start:.1f}s"
        except Exception as e:  # noqa: BLE001
            ok, detail = False, f"{type(e).__name__}: {e}"[:120]
        rows.append(
            _row(f"probe {name}", ok, detail, "llm engines", "check the key and model name")
        )
    return rows


def run(args: argparse.Namespace, settings: dict[str, Any]) -> Envelope:
    env = Envelope(command=NAME, version=__version__)
    environ = dict(os.environ)
    if args.install:
        env.results = install(args.install, environ)
        env.summary = (
            f"installed {args.install}. The launcher now uses {data_dir(environ) / 'venv'}"
        )
        return env
    env.results = checks(settings, args.quick, environ)
    if not args.quick:
        env.results += engines(settings)
    if args.probe:
        env.results += probe(settings, environ)
    for r in env.results:
        required = any(r["check"] == f"module {m}" and req for m, _, _, req in MODULES)
        if (required or r["check"] == "python") and not r["ok"]:
            env.error(MISSING, f"{r['check']} {r['detail']}", r["hint"])
    failed = sum(not r["ok"] for r in env.results)
    env.summary = f"{len(env.results) - failed} ok, {failed} not available"
    return env
