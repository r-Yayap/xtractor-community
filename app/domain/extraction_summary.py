from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import re
from typing import Mapping, Sequence


SUMMARY_LABELS = (
    "Different Page Sizes",
    "Blank Reference Number",
    "Duplicate Text Removed",
    "Multi-page PDFs",
    "Errors",
)

_EXACT_REFERENCE_ALIASES = {
    "drawing number",
    "drawing no",
    "drawing num",
    "sheet number",
    "sheet no",
    "sheet num",
    "document number",
    "document no",
    "document num",
    "reference number",
    "reference no",
    "reference num",
    "doc number",
    "doc no",
    "doc num",
    "ref number",
    "ref no",
    "ref num",
}
_REFERENCE_SUBJECTS = {"drawing", "sheet", "document", "doc", "reference", "ref"}
_NUMBER_TOKENS = {"number", "no", "num", "nbr"}


def normalized_output_title(value: object) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value).casefold()))


def find_reference_column_indexes(titles: Sequence[object]) -> tuple[int, ...]:
    normalized = [normalized_output_title(title) for title in titles]
    exact = tuple(
        index for index, title in enumerate(normalized)
        if title in _EXACT_REFERENCE_ALIASES
    )
    if exact:
        return exact
    return tuple(
        index
        for index, title in enumerate(normalized)
        if (tokens := set(title.split())) & _REFERENCE_SUBJECTS
        and tokens & _NUMBER_TOKENS
    )


def find_reference_column_index(titles: Sequence[object]) -> int | None:
    indexes = find_reference_column_indexes(titles)
    return indexes[0] if indexes else None


@dataclass(frozen=True)
class PdfExtractionFacts:
    pages: int
    different_page_size: bool = False
    blank_reference_cells: int = 0
    duplicate_text_removed: bool = False

    def to_dict(self) -> dict[str, int | bool]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> PdfExtractionFacts:
        return cls(
            pages=_required_nonnegative_int(payload, "pages"),
            different_page_size=_required_bool(payload, "different_page_size"),
            blank_reference_cells=_required_nonnegative_int(
                payload, "blank_reference_cells"
            ),
            duplicate_text_removed=_required_bool(
                payload, "duplicate_text_removed"
            ),
        )


@dataclass(frozen=True)
class ExtractionSummary:
    processed_pdfs: int
    duration_hms: str
    different_page_sizes: int | None
    blank_reference_number: int | None
    duplicate_text_removed: int
    multi_page_pdfs: int
    errors: int
    reference_column_detected: bool
    reference_size_available: bool

    def display_rows(self) -> tuple[tuple[str, str], ...]:
        return (
            (
                SUMMARY_LABELS[0],
                (
                    str(self.different_page_sizes)
                    if self.reference_size_available
                    else "N/A (no reference)"
                ),
            ),
            (
                SUMMARY_LABELS[1],
                (
                    str(self.blank_reference_number)
                    if self.reference_column_detected
                    else "N/A (no column)"
                ),
            ),
            (SUMMARY_LABELS[2], str(self.duplicate_text_removed)),
            (SUMMARY_LABELS[3], str(self.multi_page_pdfs)),
            (SUMMARY_LABELS[4], str(self.errors)),
        )
    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ExtractionSummary:
        duration_hms = payload.get("duration_hms")
        if not isinstance(duration_hms, str) or not re.fullmatch(
            r"\d{2,}:\d{2}:\d{2}", duration_hms
        ):
            raise ValueError("duration_hms must use HH:MM:SS format")
        reference_column_detected = _required_bool(
            payload, "reference_column_detected"
        )
        reference_size_available = _required_bool(
            payload, "reference_size_available"
        )
        return cls(
            processed_pdfs=_required_nonnegative_int(payload, "processed_pdfs"),
            duration_hms=duration_hms,
            different_page_sizes=_optional_nonnegative_int(
                payload, "different_page_sizes", reference_size_available
            ),
            blank_reference_number=_optional_nonnegative_int(
                payload, "blank_reference_number", reference_column_detected
            ),
            duplicate_text_removed=_required_nonnegative_int(
                payload, "duplicate_text_removed"
            ),
            multi_page_pdfs=_required_nonnegative_int(payload, "multi_page_pdfs"),
            errors=_required_nonnegative_int(payload, "errors"),
            reference_column_detected=reference_column_detected,
            reference_size_available=reference_size_available,
        )


@dataclass(frozen=True)
class ExtractionResult:
    output_path: Path
    summary: ExtractionSummary

    def to_dict(self) -> dict[str, object]:
        return {
            "output_path": str(self.output_path),
            "summary": self.summary.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ExtractionResult:
        output_path = payload.get("output_path")
        summary = payload.get("summary")
        if not isinstance(output_path, str) or not output_path.strip():
            raise ValueError("output_path must be a non-empty string")
        if not isinstance(summary, Mapping):
            raise TypeError("summary must be a mapping")
        return cls(Path(output_path), ExtractionSummary.from_dict(summary))


def summary_status(label: str, value: str) -> str:
    if value.startswith("N/A"):
        return "muted"
    try:
        count = int(value)
    except (TypeError, ValueError):
        return "muted"
    if count == 0:
        return "success"
    if label == "Errors":
        return "error"
    if label in {"Duplicate Text Removed", "Multi-page PDFs"}:
        return "info"
    return "warning"


def _required_nonnegative_int(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{key} must be a non-negative integer")
    return value


def _required_bool(payload: Mapping[str, object], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise TypeError(f"{key} must be a boolean")
    return value


def _optional_nonnegative_int(
    payload: Mapping[str, object],
    key: str,
    available: bool,
) -> int | None:
    value = payload.get(key)
    if not available:
        if value is not None:
            raise ValueError(f"{key} must be None when unavailable")
        return None
    return _required_nonnegative_int(payload, key)
