from __future__ import annotations

import statistics
import re
import unicodedata

from app.infra.pdf.models import OcrResult
from app.infra.pdf.text_normalization import canonicalize_pdf_characters


_MINIMUM_MAPPED_SPACE_SAMPLE = 24
_MINIMUM_MAPPED_SPACE_OCCURRENCES = 4
_MINIMUM_OCR_MEDIAN_CONFIDENCE = 80.0
_SUSPICIOUS_TOKEN_LIMIT = 4


def looks_malformed(value: str) -> bool:
    """Return True only for high-confidence signs of a broken Unicode map."""
    text = canonicalize_pdf_characters(value).strip()
    if not text:
        return False
    if "\ufffd" in text or any(
        unicodedata.category(character).startswith("C")
        and character not in "\r\n\t"
        for character in text
    ):
        return True

    if sum(_token_looks_corrupt(token) for token in re.split(r"\s+", text)) >= (
        _SUSPICIOUS_TOKEN_LIMIT
    ):
        return True
    return _line_looks_malformed(text) or any(
        _line_looks_malformed(line.strip()) for line in text.splitlines()
    )


def _line_looks_malformed(text: str) -> bool:
    if not text:
        return False
    if (
        len(text.split()) == 1
        and text.startswith(('"', "'"))
        and ":" in text[1:-1]
        and sum(character.isupper() for character in text[1:]) >= 2
        and sum(character.islower() for character in text[1:]) >= 6
    ):
        return True

    letters = [character for character in text if character.isalpha()]
    if len(letters) < _MINIMUM_MAPPED_SPACE_SAMPLE:
        return False
    if sum(character.isspace() for character in text) > max(1, len(text) // 50):
        return False
    if sum(character.islower() for character in letters) < len(letters) * 0.5:
        return False

    uppercase_counts: dict[str, int] = {}
    for character in letters:
        if character.isupper():
            uppercase_counts[character] = uppercase_counts.get(character, 0) + 1
    if not uppercase_counts:
        return False
    marker, count = max(uppercase_counts.items(), key=lambda item: item[1])
    if count < _MINIMUM_MAPPED_SPACE_OCCURRENCES:
        return False

    embedded = 0
    for index, character in enumerate(text):
        if character != marker:
            continue
        left = text[index - 1] if index else ""
        right = text[index + 1] if index + 1 < len(text) else ""
        if (left.islower() or left.isdigit()) and (
            right.islower() or right.isdigit()
        ):
            embedded += 1
    return embedded >= _MINIMUM_MAPPED_SPACE_OCCURRENCES


def _token_looks_corrupt(token: str) -> bool:
    letters = [character for character in token if character.isalpha()]
    if len(letters) < 4 or sum(character.islower() for character in letters) < 4:
        return False
    has_embedded_colon = bool(re.search(r"[A-Za-z]:[A-Za-z]", token))
    has_embedded_digit = bool(re.search(r"[a-z]\d|\d[a-z]", token))
    punctuation_count = sum(character in ':,."\'' for character in token)
    return has_embedded_digit or (
        has_embedded_colon
        and (len(token) >= 9 or punctuation_count >= 2)
    )


def is_high_confidence_ocr(result: OcrResult) -> bool:
    text = result.text.removeprefix("_OCR_").strip()
    if not text or not result.words or looks_malformed(text):
        return False
    return (
        statistics.median(word.confidence for word in result.words)
        >= _MINIMUM_OCR_MEDIAN_CONFIDENCE
    )
