from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Iterable


@dataclass(frozen=True, slots=True)
class PdfRect:
    x0: float
    y0: float
    x1: float
    y1: float

    @classmethod
    def from_value(cls, value: Iterable[float]) -> "PdfRect":
        x0, y0, x1, y1 = value
        return cls(float(x0), float(y0), float(x1), float(y1))

    def to_tuple(self) -> tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x1, self.y1)

    @property
    def is_finite(self) -> bool:
        return all(math.isfinite(value) for value in self.to_tuple())

    def normalized(self) -> "PdfRect":
        return PdfRect(
            min(self.x0, self.x1),
            min(self.y0, self.y1),
            max(self.x0, self.x1),
            max(self.y0, self.y1),
        )

    @property
    def width(self) -> float:
        rect = self.normalized()
        return rect.x1 - rect.x0

    @property
    def height(self) -> float:
        rect = self.normalized()
        return rect.y1 - rect.y0

    def is_valid(self, *, minimum_size: float = 0.0) -> bool:
        return (
            self.is_finite
            and self.width >= minimum_size
            and self.height >= minimum_size
            and self.width > 0.0
            and self.height > 0.0
        )

    def intersect(self, other: "PdfRect") -> "PdfRect | None":
        if not self.is_finite or not other.is_finite:
            return None
        left = self.normalized()
        right = other.normalized()
        x0 = max(left.x0, right.x0)
        y0 = max(left.y0, right.y0)
        x1 = min(left.x1, right.x1)
        y1 = min(left.y1, right.y1)
        if x1 <= x0 or y1 <= y0:
            return None
        return PdfRect(x0, y0, x1, y1)


class DiagnosticCode(str, Enum):
    NO_TEXT = "no_text"
    INVALID_CLIP = "invalid_clip"
    TEXT_EXTRACTION_FAILED = "text_extraction_failed"
    OCR_FAILED = "ocr_failed"
    FALLBACK_USED = "fallback_used"
    PAGE_DEROTATED = "page_derotated"


@dataclass(frozen=True, slots=True)
class ExtractionDiagnostic:
    code: DiagnosticCode
    message: str
    page_number: int | None = None
    requested_rect: PdfRect | None = None
    effective_rect: PdfRect | None = None
    word_count: int | None = None
    stream_bytes: int | None = None
    used_redaction: bool = False
    used_fallback: bool = False
    cause_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PdfPageInfo:
    page_number: int
    bounds: PdfRect
    media_box: PdfRect
    crop_box: PdfRect
    source_rotation_degrees: int
    rotation_degrees: int
    normalization_applied: bool
    normalization_error: str | None = None

    @property
    def width(self) -> float:
        return self.bounds.width

    @property
    def height(self) -> float:
        return self.bounds.height


@dataclass(frozen=True, slots=True)
class PdfWord:
    rect: PdfRect
    text: str
    block_number: int
    line_number: int
    word_number: int


@dataclass(frozen=True, slots=True)
class PdfCharacter:
    rect: PdfRect
    text: str
    origin: tuple[float, float]


@dataclass(frozen=True, slots=True)
class OcrWord:
    text: str
    rect: PdfRect
    confidence: float
    block_number: int
    paragraph_number: int
    line_number: int
    word_number: int


@dataclass(frozen=True, slots=True)
class BackendIdentity:
    name: str
    version: str
    engine_version: str | None = None


@dataclass(frozen=True, slots=True)
class RenderedImage:
    data: bytes
    width: int
    height: int
    mime_type: str = "image/png"
    pixel_format: str | None = None
    stride: int | None = None
    diagnostics: tuple[ExtractionDiagnostic, ...] = ()


@dataclass(frozen=True, slots=True)
class SuppressedTextOccurrence:
    text: str
    rect: PdfRect
    font: str
    font_size: float
    kept_sequence: int
    suppressed_sequence: int


@dataclass(frozen=True, slots=True)
class TextNormalizationOccurrence:
    source: str
    replacement: str
    count: int


@dataclass(frozen=True, slots=True)
class TextExtractionResult:
    text: str
    diagnostics: tuple[ExtractionDiagnostic, ...] = ()
    suppressed: tuple[SuppressedTextOccurrence, ...] = ()
    normalizations: tuple[TextNormalizationOccurrence, ...] = ()


@dataclass(frozen=True, slots=True)
class OcrResult:
    text: str
    diagnostics: tuple[ExtractionDiagnostic, ...] = ()
    words: tuple[OcrWord, ...] = ()
