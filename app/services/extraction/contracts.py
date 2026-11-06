from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol

from app.domain.models import Rect
from app.domain.area_coordinates import CoordinateBasis
from app.domain.extraction_options import (
    TextOutputPreferences,
    text_output_from_mapping,
    text_output_to_mapping,
)
from app.infra.file_metadata import FileFingerprint
from app.infra.pdf.models import (
    PdfRect,
    SuppressedTextOccurrence,
    TextNormalizationOccurrence,
)


@dataclass(frozen=True, slots=True)
class AreaExtractionPlan:
    index: int
    title: str
    rect: Rect
    use_ocr: bool
    use_image: bool


@dataclass(frozen=True, slots=True)
class ExtractionPlan:
    areas: tuple[AreaExtractionPlan, ...]
    ocr_mode: str
    ocr_enabled: bool
    ocr_dpi: int
    ocr_scale: float | None
    pdf_root: str
    text_output: TextOutputPreferences
    reference_column_index: int | None
    reference_page_width: float | None
    reference_page_height: float | None
    coordinate_basis: CoordinateBasis | None = None

    def to_mapping(self) -> dict[str, object]:
        return {
            "areas_rects": [area.rect for area in self.areas],
            "area_titles": [area.title for area in self.areas],
            "area_options": [
                (area.use_ocr, area.use_image) for area in self.areas
            ],
            "ocr_mode": self.ocr_mode,
            "ocr_enabled": self.ocr_enabled,
            "ocr_dpi": self.ocr_dpi,
            "ocr_scale": self.ocr_scale,
            "pdf_root": self.pdf_root,
            "text_output": text_output_to_mapping(self.text_output),
            "reference_column_index": self.reference_column_index,
            "reference_page_width": self.reference_page_width,
            "reference_page_height": self.reference_page_height,
            "coordinate_basis": (
                {
                    "width": self.coordinate_basis.width,
                    "height": self.coordinate_basis.height,
                }
                if self.coordinate_basis is not None
                else None
            ),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "ExtractionPlan":
        raw_rects = tuple(value.get("areas_rects", ()))
        raw_titles = tuple(value.get("area_titles", ()))
        raw_options = tuple(value.get("area_options", ()))
        if len(raw_titles) != len(raw_rects):
            raw_titles = tuple(f"Area {index + 1}" for index in range(len(raw_rects)))
        if len(raw_options) != len(raw_rects):
            mode = str(value.get("ocr_mode", "Default"))
            enabled = bool(value.get("ocr_enabled", True))
            raw_options = tuple(
                (enabled, False) if mode == "OCR-All" else (False, mode == "Text1st+Image-beta")
                for _ in raw_rects
            )
        areas = tuple(
            AreaExtractionPlan(
                index=index,
                title=str(raw_titles[index]),
                rect=_rect(raw),
                use_ocr=bool(value.get("ocr_enabled", True))
                and bool(raw_options[index][0]),
                use_image=bool(raw_options[index][1]),
            )
            for index, raw in enumerate(raw_rects)
        )
        basis_value = value.get("coordinate_basis")
        coordinate_basis = (
            CoordinateBasis(
                basis_value.get("width"),
                basis_value.get("height"),
            )
            if isinstance(basis_value, Mapping)
            else None
        )
        return cls(
            areas=areas,
            ocr_mode=str(value.get("ocr_mode") or "Default"),
            ocr_enabled=bool(value.get("ocr_enabled", True)),
            ocr_dpi=max(1, int(value.get("ocr_dpi") or 150)),
            ocr_scale=_optional_float(value.get("ocr_scale")),
            pdf_root=str(value.get("pdf_root") or "."),
            text_output=text_output_from_mapping(value.get("text_output", {})),
            reference_column_index=_optional_index(value.get("reference_column_index")),
            reference_page_width=_optional_float(value.get("reference_page_width")),
            reference_page_height=_optional_float(value.get("reference_page_height")),
            coordinate_basis=coordinate_basis,
        )


class PdfProcessor(Protocol):
    def __call__(
        self,
        pdf_path: Path,
        plan: ExtractionPlan,
        temp_dir: Path,
        prefix: str,
        *,
        facts_out: dict[str, object] | None = None,
        file_fingerprint: FileFingerprint | None = None,
    ) -> int: ...


@dataclass(frozen=True, slots=True)
class PageAreaPlan:
    area: AreaExtractionPlan
    source_rect: Rect
    text_rect: PdfRect
    image_rect: PdfRect | None


@dataclass(frozen=True, slots=True)
class PageExtractionPlan:
    page_number: int
    bounds: PdfRect
    width: float
    height: float
    rotation_degrees: int
    source_rotation_degrees: int
    areas: tuple[PageAreaPlan, ...]
    different_page_size: bool


@dataclass(frozen=True, slots=True)
class ExtractionDiagnostic:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class AreaExtractionResult:
    area: PageAreaPlan
    value: str
    source: str
    diagnostic: ExtractionDiagnostic | None = None
    suppressed: tuple[SuppressedTextOccurrence, ...] = ()
    normalizations: tuple[TextNormalizationOccurrence, ...] = ()
    staged_image: Path | None = None


@dataclass(frozen=True, slots=True)
class SuppressionAuditEvent:
    area_index: int
    area_title: str
    value: str
    rect: PdfRect
    reason: str
    font: str = ""
    font_size: float = 0.0
    kept_sequence: int = 0
    suppressed_sequence: int = 0
    replacement: str = ""
    count: int = 1


@dataclass(frozen=True, slots=True)
class PageExtractionContext:
    image_target: Callable[[int], Path | None]


@dataclass(frozen=True, slots=True)
class PageExtractionResult:
    page_plan: PageExtractionPlan
    area_values: tuple[str, ...]
    audit_events: tuple[SuppressionAuditEvent, ...]
    blank_reference: bool
    different_page_size: bool
    duplicate_text_removed: bool

    @property
    def page_size(self) -> str:
        return f"{self.page_plan.width:.1f} x {self.page_plan.height:.1f}"


def _rect(value: object) -> Rect:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError("extraction rectangle must contain four coordinates")
    return tuple(float(item) for item in value)  # type: ignore[return-value]


def _optional_index(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    return float(value)
