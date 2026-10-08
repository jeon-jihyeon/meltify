"""Map one language setting to each engine's own code"""

from __future__ import annotations

# Codes for Apple Vision and Whisper, in that order
CODES = {
    "ko": ("ko-KR", "ko"),
    "en": ("en-US", "en"),
    "ja": ("ja-JP", "ja"),
    "zh": ("zh-Hans", "zh"),
    "de": ("de-DE", "de"),
    "fr": ("fr-FR", "fr"),
    "es": ("es-ES", "es"),
}


def code(raw: str) -> str:
    """A --lang value checked against the codes every engine maps, for argparse"""
    import argparse

    lang = raw.strip().lower()
    if lang not in CODES:
        raise argparse.ArgumentTypeError(f"unknown language {raw!r}, use one of {', '.join(CODES)}")
    return lang


def _row(lang: str) -> tuple[str, str]:
    return CODES.get(lang, (lang, lang))


def vision(lang: str) -> list[str]:
    # The target language goes first, since macOS 27 Vision only applies the first one
    first = _row(lang)[0]
    return [first] if first == "en-US" else [first, "en-US"]


def whisper(lang: str) -> str | None:
    return None if lang == "auto" else _row(lang)[1]
