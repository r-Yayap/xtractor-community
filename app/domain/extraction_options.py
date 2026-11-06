from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class TextOutputPreferences:
    normalize_whitespace: bool = True
    include_occluded_text: bool = True
    include_clipped_text: bool = True
    audit_excluded_text: bool = True


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _bool(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def text_output_from_mapping(value: Any) -> TextOutputPreferences:
    raw = _mapping(value)
    defaults = TextOutputPreferences()
    return TextOutputPreferences(
        normalize_whitespace=_bool(
            raw.get("normalize_whitespace"), defaults.normalize_whitespace
        ),
        include_occluded_text=_bool(
            raw.get("include_occluded_text"), defaults.include_occluded_text
        ),
        include_clipped_text=_bool(
            raw.get("include_clipped_text"), defaults.include_clipped_text
        ),
        audit_excluded_text=_bool(
            raw.get("audit_excluded_text"), defaults.audit_excluded_text
        ),
    )


def text_output_to_mapping(value: TextOutputPreferences) -> dict[str, bool]:
    normalized = text_output_from_mapping(
        {
            "normalize_whitespace": value.normalize_whitespace,
            "include_occluded_text": value.include_occluded_text,
            "include_clipped_text": value.include_clipped_text,
            "audit_excluded_text": value.audit_excluded_text,
        }
    )
    return {
        "normalize_whitespace": normalized.normalize_whitespace,
        "include_occluded_text": normalized.include_occluded_text,
        "include_clipped_text": normalized.include_clipped_text,
        "audit_excluded_text": normalized.audit_excluded_text,
    }


OUTPUT_ORDER_WARNING = (
    "Invalid Sheet1 output column order was normalized; "
    "all required columns remain enabled."
)


def resolve_worker_count(
    profile: str,
    custom_count: int,
    *,
    cpu_count: int | None,
) -> int:
    available = cpu_count if isinstance(cpu_count, int) and not isinstance(cpu_count, bool) else 1
    available = max(1, available)

    if profile == "balanced":
        return max(1, available // 2)
    if profile == "maximum":
        return available
    if profile == "custom":
        requested = (
            custom_count
            if isinstance(custom_count, int)
            and not isinstance(custom_count, bool)
            and custom_count >= 1
            else 1
        )
        return min(requested, available)
    return available
