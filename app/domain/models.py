from dataclasses import dataclass
import math
from pathlib import Path
from collections.abc import Mapping as MappingABC
from typing import Iterable, Optional, Tuple, List

from app.domain.extraction_schema import FINAL_BASE_ORDER
from app.domain.extraction_options import TextOutputPreferences
from app.domain.output_path import OutputPathPlan
from app.domain.area_coordinates import CoordinateBasis

Rect = Tuple[float, float, float, float]
AREA_COORDINATE_KEYS = ("coordinates", "rect", "bbox")


class AreaSpecValidationError(ValueError):
    """Raised when an external area payload cannot become an AreaSpec."""


@dataclass(frozen=True)
class AreaSpec:
    title: str
    rect: Rect
    use_ocr: bool = False
    use_image: bool = False

    @classmethod
    def create(
        cls,
        title: object,
        rect: object,
        *,
        use_ocr: bool = False,
        use_image: bool = False,
        fallback_title: str = "Area",
    ) -> "AreaSpec":
        """Create a validated typed area for new runtime state."""
        normalized_title = str(title or fallback_title)
        return cls(
            normalized_title,
            _validate_area_rect(rect, normalized_title),  # type: ignore[arg-type]
            use_ocr=bool(use_ocr),
            use_image=bool(use_image),
        )

    def with_rect(self, rect: object) -> "AreaSpec":
        """Return this area with validated geometry."""
        return type(self)(
            self.title,
            _validate_area_rect(rect, self.title),  # type: ignore[arg-type]
            use_ocr=self.use_ocr,
            use_image=self.use_image,
        )

    @classmethod
    def from_mapping(
        cls,
        value: object,
        *,
        fallback_title: str = "Area",
    ) -> "AreaSpec":
        """Normalize one legacy/persisted area payload at its boundary."""
        if not isinstance(value, MappingABC):
            raise AreaSpecValidationError(
                f"{fallback_title} has an unsupported area definition."
            )

        title = str(value.get("title") or fallback_title)
        coordinates = None
        found_coordinate_key = False
        for key in AREA_COORDINATE_KEYS:
            if key not in value:
                continue
            found_coordinate_key = True
            candidate = value.get(key)
            if candidate:
                coordinates = candidate
                break
        if coordinates is None:
            if found_coordinate_key:
                raise AreaSpecValidationError(
                    f"{title} must have four coordinates."
                )
            raise AreaSpecValidationError(
                f"{title} must define coordinates."
            )
        rect = _validate_area_rect(coordinates, title)
        return cls(
            title,
            rect,  # type: ignore[arg-type]
            use_ocr=bool(value.get("use_ocr", False)),
            use_image=bool(value.get("use_image", False)),
        )


def normalize_area_spec(
    value: object,
    *,
    fallback_title: str = "Area",
) -> AreaSpec:
    """Return a typed area, adapting legacy mappings only at the boundary."""
    if isinstance(value, AreaSpec):
        _validate_area_rect(value.rect, value.title or fallback_title)
        return value
    return AreaSpec.from_mapping(value, fallback_title=fallback_title)


def _validate_area_rect(coordinates: object, title: str) -> Rect:
    if not isinstance(coordinates, (list, tuple)) or len(coordinates) != 4:
        raise AreaSpecValidationError(
            f"{title} must have four coordinates."
        )
    if any(isinstance(item, bool) for item in coordinates):
        raise AreaSpecValidationError(
            f"{title} has non-numeric coordinates."
        )
    try:
        rect = tuple(float(item) for item in coordinates)
    except (OverflowError, TypeError, ValueError):
        raise AreaSpecValidationError(
            f"{title} has non-numeric coordinates."
        ) from None
    if not all(math.isfinite(item) for item in rect):
        raise AreaSpecValidationError(
            f"{title} has non-finite coordinates."
        )
    x0, y0, x1, y1 = rect
    if x1 <= x0 or y1 <= y0:
        raise AreaSpecValidationError(
            f"{title} must have positive width and height."
        )
    return rect  # type: ignore[return-value]

@dataclass(frozen=True)
class OcrSettings:
    mode: str           # "Default" | "OCR-All" | "Text1st+Image-beta" | "Custom"
    dpi: int
    scale: Optional[float] = None  # optional upscale for images (kept for parity)
    enabled: bool = True


@dataclass(frozen=True)
class ExtractionRequest:
    pdf_paths: Iterable[Path]
    output_excel: Path
    areas: List[AreaSpec]
    ocr: OcrSettings
    pdf_root: Optional[Path] = None  # used for relative folder column (defaults to first file's parent)
    text_output: TextOutputPreferences = TextOutputPreferences()
    output_base_column_order: tuple[str, ...] = FINAL_BASE_ORDER
    worker_profile: str = "automatic"
    custom_worker_count: int = 1
    coordinate_basis: Optional[CoordinateBasis] = None
    reference_page_width: Optional[float] = None
    reference_page_height: Optional[float] = None
    output_plan: OutputPathPlan | None = None
