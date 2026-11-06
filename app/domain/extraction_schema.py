"""Canonical schemas for staged extraction rows and final Sheet1 columns.

The worker writes the staged order defined here.  Consumers should use the
helpers in this module instead of reproducing metadata offsets locally.
"""

from __future__ import annotations

from collections.abc import Mapping as MappingABC
from dataclasses import dataclass
from typing import Mapping, Sequence


STAGED_FILE_COLUMNS = (
    "UNID",
    "SHA256",
    "Size (Bytes)",
    "Date Last Modified",
    "Folder",
    "Filename",
    "Page No",
    "Page Size",
    "Error",
)

FINAL_BASE_ORDER = (
    "UNID",
    "SHA256",
    "Size (Bytes)",
    "Date Last Modified",
    "Page No",
    "Page Size",
    "Open PDF",
    "Folder",
    "Filename",
    "Error",
)

PREVIOUS_DEFAULT_BASE_ORDER = (
    "UNID", "SHA256", "Size (Bytes)", "Date Last Modified",
    "Page No", "Page Size", "Folder", "Filename", "Open PDF", "Error",
)

FINAL_COMPUTED_COLUMNS = ("Open PDF",)


def normalize_final_base_order(
    value: Sequence[object] | None,
) -> tuple[str, ...]:
    """Return a complete, canonical first-occurrence field permutation."""
    if value is None:
        return FINAL_BASE_ORDER
    if isinstance(value, (str, bytes)) or isinstance(value, MappingABC):
        return FINAL_BASE_ORDER
    try:
        values = iter(value)
    except TypeError:
        values = iter(())
    known = set(FINAL_BASE_ORDER)
    selected: list[str] = []
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, str):
            continue
        field = item.strip()
        if field in known and field not in seen:
            selected.append(field)
            seen.add(field)
    # Preserve the relative order of all pre-existing fields when loading a
    # saved layout from before Open PDF existed.  The new final-only field is
    # inserted immediately before Folder instead of being appended at the end.
    selected.extend(
        field
        for field in FINAL_BASE_ORDER
        if field != "Open PDF" and field not in seen
    )
    if "Open PDF" not in seen:
        selected.insert(selected.index("Folder"), "Open PDF")
    if tuple(selected) == PREVIOUS_DEFAULT_BASE_ORDER:
        return FINAL_BASE_ORDER
    return tuple(selected)


def final_base_value(name: str, file_values: Mapping[str, object]) -> object:
    """Return one final base value, reserving synthetic fields explicitly."""

    if name in FINAL_COMPUTED_COLUMNS:
        return None
    return file_values[name]

# Read-only compatibility for older staging CSVs. New extraction
# output never uses this order.
LEGACY_STAGED_FILE_COLUMNS = (
    "UNID",
    "Size (Bytes)",
    "Date Last Modified",
    "Folder",
    "Filename",
    "Page No",
    "Page Size",
    "Error",
)

_STAGED_INDEX = {name: index for index, name in enumerate(STAGED_FILE_COLUMNS)}


@dataclass(frozen=True, slots=True)
class ParsedStagedRow:
    file_values: dict[str, object]
    area_values: tuple[object, ...]


def staged_header(area_headers: Sequence[str]) -> list[str]:
    return [*STAGED_FILE_COLUMNS, *area_headers]


def final_sheet_headers(
    area_headers: Sequence[str],
    *,
    include_error: bool,
    base_order: Sequence[object] | None = None,
) -> list[str]:
    order = normalize_final_base_order(base_order)
    base = [name for name in order if include_error or name != "Error"]
    return [*base, *area_headers]


def final_base_columns(
    *,
    include_error: bool,
    base_order: Sequence[object] | None = None,
) -> tuple[str, ...]:
    """Return the active structural base columns in final Sheet1 order."""
    order = normalize_final_base_order(base_order)
    return tuple(
        name for name in order if include_error or name != "Error"
    )


def final_base_column_index(
    name: str,
    *,
    include_error: bool,
    base_order: Sequence[object] | None = None,
) -> int:
    """Return a structural final-column index without visible-name searching."""
    try:
        return final_base_columns(
            include_error=include_error,
            base_order=base_order,
        ).index(name)
    except ValueError as exc:
        raise ValueError(f"Unknown or inactive final base column: {name}") from exc


def final_area_column_indices(
    area_count: int,
    *,
    include_error: bool,
    base_order: Sequence[object] | None = None,
) -> tuple[int, ...]:
    """Return final Sheet1 area indexes by structural ordinal."""
    if area_count < 0:
        raise ValueError("area_count cannot be negative")
    first_area = len(
        final_base_columns(
            include_error=include_error,
            base_order=base_order,
        )
    )
    return tuple(range(first_area, first_area + area_count))


def build_staged_row(
    *,
    unid: object,
    sha256: object,
    size_bytes: object,
    modified_display: object,
    folder: object,
    filename: object,
    page_no: object,
    page_size: object,
    error: object,
    area_values: Sequence[object],
) -> list[object]:
    return [
        unid,
        sha256,
        size_bytes,
        modified_display,
        folder,
        filename,
        page_no,
        page_size,
        error,
        *area_values,
    ]


def parse_staged_row(row: Sequence[object], area_count: int) -> ParsedStagedRow:
    """Parse a staged row using schema lengths, never caller-owned offsets."""
    if area_count < 0:
        raise ValueError("area_count cannot be negative")
    metadata_end = len(STAGED_FILE_COLUMNS)
    areas_end = metadata_end + area_count
    values = list(row)
    padded = values + [""] * max(0, areas_end - len(values))
    file_values = {
        name: padded[index] for name, index in _STAGED_INDEX.items()
    }
    return ParsedStagedRow(
        file_values=file_values,
        area_values=tuple(padded[metadata_end:areas_end]),
    )


def parse_staged_csv_row(
    row: Sequence[object],
    headers: Sequence[str],
    area_headers: Sequence[str],
) -> ParsedStagedRow:
    """Parse a CSV row by its canonical header names.

    The normal combiner emits this exact schema. Header lookup also makes the
    Excel boundary tolerant of a reordered incoming CSV without moving any
    positional knowledge into the writer.
    """
    normalized_headers = tuple(str(name) for name in headers)
    occurrences: dict[str, list[int]] = {}
    for index, name in enumerate(normalized_headers):
        occurrences.setdefault(name, []).append(index)
    next_occurrence: dict[str, int] = {}

    def take(name: str) -> object:
        indexes = occurrences.get(name, [])
        occurrence = next_occurrence.get(name, 0)
        if occurrence >= len(indexes):
            raise KeyError(name)
        index = indexes[occurrence]
        next_occurrence[name] = occurrence + 1
        return row[index] if index < len(row) else ""

    try:
        # Consume each structural region in canonical order. This preserves
        # duplicate visible names such as system Filename + area Filename.
        file_values = {name: take(name) for name in STAGED_FILE_COLUMNS}
        area_values = tuple(take(name) for name in area_headers)
    except KeyError:
        return parse_staged_row(row, len(area_headers))

    return ParsedStagedRow(
        file_values=file_values,
        area_values=area_values,
    )


def staged_file_values(values: Mapping[str, object]) -> tuple[object, ...]:
    """Return metadata values in the canonical staged order."""
    missing = [name for name in STAGED_FILE_COLUMNS if name not in values]
    if missing:
        raise ValueError(f"Missing staged file columns: {', '.join(missing)}")
    return tuple(values[name] for name in STAGED_FILE_COLUMNS)


def staged_identity_values(row: Sequence[object]) -> tuple[str, str]:
    """Return folder/filename for staged output, including legacy rows.

    Comparison reports created before SHA256 was introduced contain the old
    seven-field metadata block. Keeping that compatibility in the schema
    module prevents downstream readers from carrying a second layout.
    """
    if _is_canonical_staged_row(row):
        return (
            str(row[_STAGED_INDEX["Folder"]]),
            str(row[_STAGED_INDEX["Filename"]]),
        )
    legacy_folder_index = LEGACY_STAGED_FILE_COLUMNS.index("Folder")
    legacy_filename_index = LEGACY_STAGED_FILE_COLUMNS.index("Filename")
    return (
        str(row[legacy_folder_index]) if len(row) > legacy_folder_index else "",
        str(row[legacy_filename_index]) if len(row) > legacy_filename_index else "",
    )


def file_id_from_unid(unid: object) -> str:
    """Extract the stable file identity prefix from a page/error UNID."""

    text = str(unid or "")
    head, separator, _suffix = text.rpartition("-")
    return head if separator else text


def _looks_like_sha256(value: object) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(character in "0123456789abcdefABCDEF" for character in text)


def _is_canonical_staged_row(row: Sequence[object]) -> bool:
    """Recognize canonical rows even when the optional hash is unavailable."""
    if len(row) <= _STAGED_INDEX["Filename"]:
        return False
    if tuple(str(value) for value in row[: len(STAGED_FILE_COLUMNS)]) == STAGED_FILE_COLUMNS:
        return True
    if _looks_like_sha256(row[_STAGED_INDEX["SHA256"]]):
        return True
    # A failed fingerprint leaves SHA256 blank, but the canonical size and
    # page-number fields still distinguish the row from the legacy layout.
    size_value = _text_value(row[_STAGED_INDEX["Size (Bytes)"]])
    page_value = _text_value(row[_STAGED_INDEX["Page No"]])
    page_size_value = _text_value(row[_STAGED_INDEX["Page Size"]])
    return (
        _looks_numeric(size_value)
        and (_looks_numeric(page_value) or not page_value and not page_size_value)
    )


def _looks_numeric(value: object) -> bool:
    try:
        float(str(value).strip())
    except (TypeError, ValueError):
        return False
    return bool(str(value).strip())


def _text_value(value: object) -> str:
    return "" if value is None else str(value).strip()
