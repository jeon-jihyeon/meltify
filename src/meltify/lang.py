"""Map one language setting to each engine's own code"""

from __future__ import annotations

# Codes for Apple Vision, PaddleOCR and Whisper, in that order
CODES = {
    "ko": ("ko-KR", "korean", "ko"),
    "en": ("en-US", "en", "en"),
    "ja": ("ja-JP", "japan", "ja"),
    "zh": ("zh-Hans", "ch", "zh"),
    "de": ("de-DE", "german", "de"),
    "fr": ("fr-FR", "french", "fr"),
    "es": ("es-ES", "es", "es"),
}


def code(raw: str) -> str:
    """A --lang value checked against the codes every engine maps, for argparse"""
    import argparse

    lang = raw.strip().lower()
    if lang not in CODES:
        raise argparse.ArgumentTypeError(f"unknown language {raw!r}, use one of {', '.join(CODES)}")
    return lang


def _row(lang: str) -> tuple[str, str, str]:
    return CODES.get(lang, (lang, lang, lang))


def vision(lang: str) -> list[str]:
    # The target language goes first, since macOS 27 Vision only applies the first one
    first = _row(lang)[0]
    return [first] if first == "en-US" else [first, "en-US"]


def paddle(lang: str) -> str:
    return _row(lang)[1]


def whisper(lang: str) -> str | None:
    return None if lang == "auto" else _row(lang)[2]
