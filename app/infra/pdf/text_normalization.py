from __future__ import annotations

from collections import Counter
import re

from app.infra.pdf.models import TextNormalizationOccurrence


_REPLACEMENTS = {
    "\x02": "-",
    "\u00ad": "-",
    "\ufffe": "-",
    "\u00a0": " ",
}


def canonicalize_pdf_characters(value: str) -> str:
    return "".join(_REPLACEMENTS.get(character, character) for character in value)


def normalize_pdf_text(value: str) -> str:
    value = canonicalize_pdf_characters(value)
    value = value.replace("\r", " ").replace("\n", " ")
    value = re.sub(r"[\x00-\x1F\x7F-\x9F]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def collect_text_normalizations(value: str) -> tuple[TextNormalizationOccurrence, ...]:
    counts = Counter(character for character in value if character in _REPLACEMENTS)
    return tuple(
        TextNormalizationOccurrence(
            source=character,
            replacement=_REPLACEMENTS[character],
            count=count,
        )
        for character, count in counts.items()
    )


def character_label(value: str) -> str:
    names = {
        "\x02": "START OF TEXT",
        "\u00a0": "NO-BREAK SPACE",
        "\u00ad": "SOFT HYPHEN",
        "\ufffe": "NONCHARACTER",
        " ": "SPACE",
        "-": "HYPHEN-MINUS",
    }
    return f"U+{ord(value):04X} {names.get(value, 'CHARACTER')}"
