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
from meltify.needs import error_note

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
    ("markitdown", "read docx pptx msg epub", "meltify doctor --install office", False),
    ("olefile", "read msg ppt", "meltify doctor --install office", False),
    (
        "metafile_render",
        "read emf wmf without LibreOffice",
        "meltify doctor --install office",
        False,
    ),
    ("python_calamine", "read xls xlsb, faster xlsx", "meltify doctor --install office", False),
    ("legacy_doc", "read doc", "meltify doctor --install office", False),
    ("libarchive", "read 7z rar", "meltify doctor --install archive", False),
    ("msoffcrypto", "read encrypted office", "meltify doctor --install crypto", False),
    ("pyzipper", "read aes zip", "meltify doctor --install crypto", False),
    ("cryptography", "read encrypted hwp iwork", "meltify doctor --install crypto", False),
    ("numbers_parser", "read numbers", "meltify doctor --install iwork", False),
    ("pyarrow", "read parquet", "meltify doctor --install parquet", False),
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
    ("ffprobe", "media read fallback", "installed with ffmpeg"),
    ("deno", "media youtube", "brew install deno"),
    ("whisper-cli", "asr engine whispercpp", "brew install whisper-cpp"),
    ("wpd2text", "read wpd", "brew install libwpd or apt install libwpd-tools"),
    ("uv", "launcher and --install", "https://docs.astral.sh/uv/"),
]
SOFFICE_HINT = (
    "meltify doctor --install libreoffice, brew install --cask libreoffice "
    "or apt install libreoffice"
)
SEVEN_ZIP_HINT = "meltify doctor --install archive"
# Where Playwright's `channel="chrome"` looks first, so --render can skip its own Chromium
CHROME = (
    Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    Path("/opt/google/chrome/chrome"),
)
EXTRAS = [
    "office",
    "archive",
    "iwork",
    "parquet",
    "crypto",
    "render",
    "media",
    "asr-mlx",
    "ocr-paddle",
    "all",
    # Not a Python extra: a portable LibreOffice downloaded into the tools dir
    "libreoffice",
]


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--quick", action="store_true", help="skip binaries and optional modules")
    p.add_argument(
        "--install",
        choices=EXTRAS,
        help="install an optional extra into the launcher's venv in the data dir, or "
        "download a portable LibreOffice with `libreoffice`",
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
        from meltify.converters import run

        # So the quicklook row reports what read would do with these settings
        looks = bool(settings.get("render", {}).get("quicklook", True))
        run.use(run.RunContext(quicklook=looks))
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


def _where(binary: str) -> str:
    from meltify import tools

    return f"{binary} (downloaded)" if Path(binary).is_relative_to(tools.tools_dir()) else binary


def system_parts() -> list[dict[str, Any]]:
    """Programs and shared libraries outside the venv that some formats lean on"""
    from meltify import tools
    from meltify.converters import quicklook, render
    from meltify.converters.archive import libarchive_ready

    binary = render.soffice()
    rows = [
        _row(
            "bin soffice",
            binary is not None,
            "not found" if binary is None else _where(binary),
            "read PowerPoint 95, formula recalc, emf pictures, uncached charts, other binaries",
            SOFFICE_HINT,
        )
    ]
    if IS_MAC:
        ready = quicklook.available()
        rows.append(
            _row(
                "render quicklook",
                ready,
                "qlmanage" if ready else "turned off or qlmanage missing",
                "read renders when soffice is missing or fails",
                "set render.quicklook = true",
            )
        )
    seven = tools.seven_zip()
    rows.append(
        _row(
            "bin 7zz",
            seven is not None,
            "not found" if seven is None else _where(seven),
            "read 7z rar and other archives",
            SEVEN_ZIP_HINT,
        )
    )
    # libarchive-c is only a binding, so the shared library has to be there as well
    if importlib.util.find_spec("libarchive") is not None:
        ready = libarchive_ready()
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


def _size(n: int) -> str:
    return f"{n / 2**20:.0f} MB"


def install_tool(name: str, env: dict[str, str]) -> dict[str, Any]:
    """Download one pinned helper binary into the tools dir"""
    from meltify import tools

    root = data_dir(env) / "tools"
    if name == "libreoffice":
        item = tools.pinned(tools.LIBREOFFICE, "LibreOffice")
        what, used_by = f"LibreOffice {tools.LIBREOFFICE_VERSION}", "read renders"
        install = tools.install_libreoffice
    else:
        item = tools.pinned(tools.SEVEN_ZIP, "7-Zip")
        what, used_by = f"7-Zip {tools.SEVEN_ZIP_VERSION}", "read archives"
        install = tools.install_seven_zip
    # The download can take minutes, so say what's coming before it starts
    print(f"downloading {what} ({_size(item.size)}) into {root}", file=sys.stderr)
    binary = install(root)
    return _row(f"install {name}", True, f"{what}, {_size(item.size)}, at {binary}", used_by, "")


def install(extra: str, env: dict[str, str]) -> list[dict[str, Any]]:
    from meltify import safe

    if extra == "libreoffice":
        return [install_tool("libreoffice", env)]
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
    if extra in ("archive", "all"):
        try:
            rows.append(install_tool("7zip", env))
        except (OSError, RuntimeError) as e:
            # 7-Zip only adds encrypted archives, so a platform without a pinned build or a
            # failed download must not undo the Python install above
            rows.append(
                _row(
                    "install 7zip",
                    False,
                    error_note(e),
                    "read archives",
                    f"put 7zz or 7z on PATH, or rerun {SEVEN_ZIP_HINT} once online",
                )
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
            ok, detail = False, error_note(e, 120)
        rows.append(
            _row(f"probe {name}", ok, detail, "llm engines", "check the key and model name")
        )
    return rows


def run(args: argparse.Namespace, settings: dict[str, Any]) -> Envelope:
    env = Envelope(command=NAME, version=__version__)
    environ = dict(os.environ)
    if args.install:
        env.results = install(args.install, environ)
        env.warnings += [
            f"{r['check']}: {r['detail']} ({r['hint']})" for r in env.results if not r["ok"]
        ]
        if args.install == "libreoffice":
            env.summary = "installed libreoffice. read now renders with it"
        else:
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
