"""Vision calls over plain REST, so you don't need any provider SDK"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

from meltify.safe import MissingTool

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


def _b64(path: Path) -> str:
    return base64.standard_b64encode(path.read_bytes()).decode()


def _mime(path: Path) -> str:
    return "image/jpeg" if path.suffix.lower() in {".jpg", ".jpeg"} else "image/png"


def anthropic_vision(image: Path, model: str, api_key: str, prompt: str = TRANSCRIBE) -> str:
    import httpx

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
    r = httpx.post(
        "https://api.anthropic.com/v1/messages",
        json=body,
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return "".join(c.get("text", "") for c in r.json()["content"])


def gemini_vision(image: Path, model: str, api_key: str, prompt: str = TRANSCRIBE) -> str:
    import httpx

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
    r = httpx.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        json=body,
        headers={"x-goog-api-key": api_key},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    parts = r.json()["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts)


def openai_vision(
    image: Path, model: str, api_key: str, base_url: str, prompt: str = TRANSCRIBE
) -> str:
    import httpx

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
    r = httpx.post(
        f"{base_url.rstrip('/')}/chat/completions",
        json=body,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]
