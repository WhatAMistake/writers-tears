from langdetect import detect_langs, DetectorFactory
import re

DetectorFactory.seed = 0
_CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")


def _contains_cyrillic(text: str) -> bool:
    return bool(_CYRILLIC_RE.search(text))


def detect_language(text: str):
    """Return (lang_code, probability). Cyrillic -> 'ru' with high confidence."""
    if not text or not text.strip():
        return None, 0.0
    if _contains_cyrillic(text):
        return "ru", 0.99
    try:
        langs = detect_langs(text)
        if not langs:
            return None, 0.0
        top = langs[0]
        return top.lang, float(top.prob)
    except Exception as e:
        print(f"Language detection error: {e}")
        return None, 0.0
