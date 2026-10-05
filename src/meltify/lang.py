"""Map one language setting to each engine's own code"""

from __future__ import annotations

# Codes for Apple Vision, PaddleOCR, Whisper and subtitles, in that order
CODES = {
    "ko": ("ko-KR", "korean", "ko", "ko"),
    "en": ("en-US", "en", "en", "en"),
    "ja": ("ja-JP", "japan", "ja", "ja"),
    "zh": ("zh-Hans", "ch", "zh", "zh-Hans"),
    "de": ("de-DE", "german", "de", "de"),
    "fr": ("fr-FR", "french", "fr", "fr"),
    "es": ("es-ES", "es", "es", "es"),
}


def _row(lang: str) -> tuple[str, str, str, str]:
    return CODES.get(lang, (lang, lang, lang, lang))


def vision(lang: str) -> list[str]:
    # The target language goes first, since macOS 27 Vision only applies the first one
    first = _row(lang)[0]
    return [first] if first == "en-US" else [first, "en-US"]


def paddle(lang: str) -> str:
    return _row(lang)[1]


def whisper(lang: str) -> str | None:
    return None if lang == "auto" else _row(lang)[2]


def subtitles(lang: str) -> str:
    return _row(lang)[3]
