"""Vision and speech calls over plain REST, so you don't need any provider SDK"""

from __future__ import annotations

import base64
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meltify.safe import MissingTool, Unreachable

TIMEOUT = 120.0
TRANSCRIBE = (
    "Transcribe every piece of visible text exactly as written, one line of the image per line. "
    "Keep numbers, units and punctuation. Do not correct spelling, translate or guess. "
    "Write [?] for any character you cannot read. Output only the transcription."
)


def key(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise MissingTool(name, f"export {name}=... or pick a local OCR engine")
    return value


@dataclass(frozen=True)
class Endpoint:
    """An OpenAI-compatible server running a model you picked, on your machine or network"""

    name: str
    base_url: str
    model: str
    # Most servers you run take no key, so an unset variable sends none
    key_env: str = ""
    timeout: float = TIMEOUT
    prompt: str = TRANSCRIBE

    def missing(self) -> str | None:
        from meltify import fetch

        # A hosted API must never run unnamed, so only private hosts count as yours
        if not fetch.private_only(self.base_url):
            return f"an endpoint needs a loopback or private address, not {self.base_url}"
        if self.key_env and not os.environ.get(self.key_env):
            return f"export {self.key_env}=..."
        return None

    @property
    def api_key(self) -> str:
        return os.environ.get(self.key_env, "") if self.key_env else ""


FIELDS = {"base_url": str, "model": str, "key_env": str, "timeout": (int, float), "prompt": str}


def endpoints(
    section: str,
    tables: Mapping[str, Any],
    taken: set[str],
    timeout: float,
    fields: Mapping[str, Any] = FIELDS,
) -> dict[str, Endpoint]:
    """The `[SECTION.endpoints.NAME]` tables, checked so a typo fails loudly"""
    found = {}
    for name, table in tables.items():
        where = f"{section}.endpoints.{name}"
        # The name lands in cache file names and comma-separated engine lists
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ValueError(f"{where}: use letters, digits, - and _ in the name")
        if name in taken:
            raise ValueError(f"{where}: {name} is a built-in engine, pick another name")
        if not isinstance(table, Mapping):
            raise ValueError(f"{where} must be a table with base_url and model")
        for field, value in table.items():
            kind = fields.get(field)
            if kind is None:
                raise ValueError(f"{where}: unknown field {field}, use {', '.join(fields)}")
            if not isinstance(value, kind) or isinstance(value, bool):
                raise ValueError(f"{where}.{field} has the wrong type")
        if not table.get("base_url") or not table.get("model"):
            raise ValueError(f"{where} needs base_url and model")
        found[name] = Endpoint(name, **{"timeout": timeout, **table})
    return found


def bearer(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def post(url: str, **kwargs: Any) -> Any:
    """httpx.post, raising Unreachable when nothing answers at `url`"""
    import httpx

    try:
        r = httpx.post(url, **kwargs)
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        raise Unreachable(f"nothing answers at {url}: {e}") from e
    r.raise_for_status()
    return r


def answers(base_url: str, api_key: str = "") -> str | None:
    """Why the server at `base_url` isn't usable, or None when it lists its models"""
    import httpx

    try:
        r = httpx.get(f"{base_url.rstrip('/')}/models", headers=bearer(api_key), timeout=5)
    except httpx.HTTPError as e:
        return f"not answering at {base_url}: {type(e).__name__}"
    return None if r.status_code < 400 else f"http {r.status_code} from {base_url}/models"


def _b64(path: Path) -> str:
    return base64.standard_b64encode(path.read_bytes()).decode()


def _mime(path: Path) -> str:
    return "image/jpeg" if path.suffix.lower() in {".jpg", ".jpeg"} else "image/png"


def anthropic_vision(image: Path, model: str, api_key: str, prompt: str = TRANSCRIBE) -> str:
    body = {
        "model": model,
        "max_tokens": 4096,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": _mime(image),
                            "data": _b64(image),
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    }
    r = post(
        "https://api.anthropic.com/v1/messages",
        json=body,
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
        timeout=TIMEOUT,
    )
    return "".join(c.get("text", "") for c in r.json()["content"])


def gemini_vision(image: Path, model: str, api_key: str, prompt: str = TRANSCRIBE) -> str:
    body = {
        "contents": [
            {
                "parts": [
                    {"inline_data": {"mime_type": _mime(image), "data": _b64(image)}},
                    {"text": prompt},
                ]
            }
        ]
    }
    r = post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        json=body,
        headers={"x-goog-api-key": api_key},
        timeout=TIMEOUT,
    )
    parts = r.json()["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts)


def openai_vision(
    image: Path,
    model: str,
    api_key: str,
    base_url: str,
    prompt: str = TRANSCRIBE,
    timeout: float = TIMEOUT,
) -> str:
    data_uri = f"data:{_mime(image)};base64,{_b64(image)}"
    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_uri}},
                ],
            }
        ],
    }
    r = post(
        f"{base_url.rstrip('/')}/chat/completions",
        json=body,
        headers=bearer(api_key),
        timeout=timeout,
    )
    return r.json()["choices"][0]["message"]["content"]
