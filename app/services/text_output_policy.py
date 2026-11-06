from __future__ import annotations

from dataclasses import dataclass
import re

from app.domain.extraction_options import TextOutputPreferences
from app.infra.pdf.text_normalization import (
    canonicalize_pdf_characters,
    normalize_pdf_text,
)


_LABEL_LINE = re.compile(
    r"^\[(?P<possible>POSSIBLY\s+)?(?P<category>OCCLUDED|CLIPPED)\]\s*(?P<text>.*)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ExcludedLabeledText:
    classification: str
    value: str


@dataclass(frozen=True)
class TextOutputResult:
    value: str
    excluded: tuple[ExcludedLabeledText, ...] = ()


def _normalized_line(value: str) -> str:
    return normalize_pdf_text(value)


def apply_text_output_policy(
    value: object,
    preferences: TextOutputPreferences | None = None,
) -> TextOutputResult:
    preferences = preferences or TextOutputPreferences()
    source = canonicalize_pdf_characters("" if value is None else str(value))
    source = source.replace("\r\n", "\n").replace("\r", "\n")

    output_segments: list[str] = []
    ordinary_lines: list[str] = []
    excluded: list[ExcludedLabeledText] = []

    def flush_ordinary() -> None:
        if not ordinary_lines:
            return
        if preferences.normalize_whitespace:
            segment = _normalized_line(" ".join(ordinary_lines))
        else:
            segment = "\n".join(ordinary_lines).strip()
        ordinary_lines.clear()
        if segment:
            output_segments.append(segment)

    for raw_line in source.split("\n"):
        stripped = raw_line.strip()
        match = _LABEL_LINE.fullmatch(stripped)
        if match is None:
            ordinary_lines.append(raw_line)
            continue

        flush_ordinary()
        category = match.group("category").casefold()
        possible = bool(match.group("possible"))
        classification = f"possibly {category}" if possible else category
        label = f"[{'POSSIBLY ' if possible else ''}{category.upper()}]"
        label_text = _normalized_line(match.group("text"))
        labeled_value = f"{label} {label_text}".rstrip()
        include = (
            preferences.include_occluded_text
            if category == "occluded"
            else preferences.include_clipped_text
        )
        if include:
            output_segments.append(labeled_value)
        else:
            excluded.append(ExcludedLabeledText(classification, labeled_value))

    flush_ordinary()
    return TextOutputResult("\n".join(output_segments), tuple(excluded))
