from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum


class OcrOutcome(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OcrSegmentationMode(int, Enum):
    AUTO = 3
    SINGLE_LINE = 7
    SINGLE_CHARACTER = 10


class OcrDiagnosticCode(str, Enum):
    EXECUTABLE_UNAVAILABLE = "executable_unavailable"
    TESSDATA_UNAVAILABLE = "tessdata_unavailable"
    RUNTIME_INVALID = "runtime_invalid"
    PROCESS_LAUNCH_FAILED = "process_launch_failed"
    PROCESS_TIMEOUT = "process_timeout"
    PROCESS_EXIT_FAILED = "process_exit_failed"
    PROCESS_TERMINATED = "process_terminated"
    OUTPUT_MISSING = "output_missing"
    OUTPUT_TOO_LARGE = "output_too_large"
    MALFORMED_OUTPUT = "malformed_output"
    INVALID_ENCODING = "invalid_encoding"
    CANCELLED = "cancelled"
    INPUT_TOO_LARGE = "input_too_large"
    INVALID_REQUEST = "invalid_request"
    RENDERER_FAILED = "renderer_failed"
    RETRY_EXHAUSTED = "retry_exhausted"
    CONCURRENCY_CANCELLED = "concurrency_cancelled"
    CONCURRENCY_TIMEOUT = "concurrency_timeout"
    GROUPING_AMBIGUOUS = "grouping_ambiguous"
    INTERNAL_FAILURE = "internal_failure"


@dataclass(frozen=True, slots=True)
class OcrRect:
    x0: float
    y0: float
    x1: float
    y1: float

    def __post_init__(self) -> None:
        values = (self.x0, self.y0, self.x1, self.y1)
        if not all(math.isfinite(float(value)) for value in values):
            raise ValueError("OCR rectangle coordinates must be finite")
        if self.x1 < self.x0 or self.y1 < self.y0:
            raise ValueError("OCR rectangle coordinates must be ordered")

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0


@dataclass(frozen=True, slots=True)
class OcrTransform:
    """Axis-aligned transform from OCR image pixels to Xtractor page space."""

    scale_x: float
    scale_y: float
    offset_x: float = 0.0
    offset_y: float = 0.0

    def __post_init__(self) -> None:
        values = (self.scale_x, self.scale_y, self.offset_x, self.offset_y)
        if not all(math.isfinite(float(value)) for value in values):
            raise ValueError("OCR transform values must be finite")
        if self.scale_x <= 0 or self.scale_y <= 0:
            raise ValueError("OCR transform scales must be positive")

    def map_rect(self, rect: OcrRect) -> OcrRect:
        return OcrRect(
            self.offset_x + rect.x0 * self.scale_x,
            self.offset_y + rect.y0 * self.scale_y,
            self.offset_x + rect.x1 * self.scale_x,
            self.offset_y + rect.y1 * self.scale_y,
        )


@dataclass(frozen=True, slots=True)
class OcrImage:
    data: bytes
    width: int
    height: int
    mime_type: str = "image/png"

    def __post_init__(self) -> None:
        if not isinstance(self.data, bytes):
            raise TypeError("OCR image data must be owned bytes")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("OCR image dimensions must be positive")
        if self.mime_type != "image/png":
            raise ValueError("The initial OCR runtime accepts PNG images only")


@dataclass(frozen=True, slots=True)
class OcrRequest:
    image: OcrImage
    transform: OcrTransform
    operation_id: str
    source_id: str | None = None
    page_number: int | None = None
    area_id: str | None = None
    language: str = "eng"
    segmentation_mode: OcrSegmentationMode = OcrSegmentationMode.AUTO

    def __post_init__(self) -> None:
        if not self.operation_id.strip():
            raise ValueError("OCR operation ID is required")
        if self.page_number is not None and self.page_number < 0:
            raise ValueError("OCR page number must be non-negative")
        if self.language != "eng":
            raise ValueError("Only the audited English OCR model is supported")


@dataclass(frozen=True, slots=True)
class OcrWord:
    text: str
    rect: OcrRect
    confidence: float
    block_number: int
    paragraph_number: int
    line_number: int
    word_number: int

    def __post_init__(self) -> None:
        if not self.text:
            raise ValueError("OCR words cannot be empty")
        if not math.isfinite(self.confidence) or not 0 <= self.confidence <= 100:
            raise ValueError("OCR confidence must be between 0 and 100")
        if min(
            self.block_number,
            self.paragraph_number,
            self.line_number,
            self.word_number,
        ) < 0:
            raise ValueError("OCR hierarchy numbers must be non-negative")


@dataclass(frozen=True, slots=True)
class OcrDiagnostic:
    code: OcrDiagnosticCode
    message: str
    retryable: bool = False
    exit_code: int | None = None
    retry_count: int = 0
    timed_out: bool = False

    def __post_init__(self) -> None:
        if not self.message.strip():
            raise ValueError("OCR diagnostics require a safe message")
        if self.retry_count < 0:
            raise ValueError("OCR retry count cannot be negative")


@dataclass(frozen=True, slots=True)
class OcrResult:
    outcome: OcrOutcome
    text: str | None
    words: tuple[OcrWord, ...] = ()
    diagnostics: tuple[OcrDiagnostic, ...] = ()
    elapsed_ms: float = 0.0
    attempts: int = 0
    runtime_version: str | None = None
    peak_child_memory_bytes: int = 0

    def __post_init__(self) -> None:
        if not math.isfinite(self.elapsed_ms) or self.elapsed_ms < 0:
            raise ValueError("OCR elapsed time must be finite and non-negative")
        if self.attempts < 0:
            raise ValueError("OCR attempt count cannot be negative")
        if self.peak_child_memory_bytes < 0:
            raise ValueError("OCR child peak memory cannot be negative")
        if self.outcome is OcrOutcome.SUCCESS:
            if self.text is None:
                raise ValueError("A successful OCR result requires text")
        else:
            if self.text is not None or self.words:
                raise ValueError("A failed OCR result cannot carry business text")
            if not self.diagnostics:
                raise ValueError("A failed OCR result requires a diagnostic")

    @property
    def succeeded(self) -> bool:
        return self.outcome is OcrOutcome.SUCCESS

    @classmethod
    def success(
        cls,
        text: str,
        *,
        words: tuple[OcrWord, ...] = (),
        diagnostics: tuple[OcrDiagnostic, ...] = (),
        elapsed_ms: float = 0.0,
        attempts: int = 0,
        runtime_version: str | None = None,
        peak_child_memory_bytes: int = 0,
    ) -> "OcrResult":
        return cls(
            OcrOutcome.SUCCESS,
            text,
            words,
            diagnostics,
            elapsed_ms,
            attempts,
            runtime_version,
            peak_child_memory_bytes,
        )

    @classmethod
    def failure(
        cls,
        diagnostic: OcrDiagnostic,
        *,
        elapsed_ms: float = 0.0,
        attempts: int = 0,
    ) -> "OcrResult":
        outcome = (
            OcrOutcome.CANCELLED
            if diagnostic.code
            in {OcrDiagnosticCode.CANCELLED, OcrDiagnosticCode.CONCURRENCY_CANCELLED}
            else OcrOutcome.FAILED
        )
        return cls(outcome, None, (), (diagnostic,), elapsed_ms, attempts)


@dataclass(frozen=True, slots=True)
class OcrPolicy:
    timeout_seconds: float = 30.0
    termination_grace_seconds: float = 1.0
    max_concurrent_operations: int = 2
    max_retries: int = 1
    max_image_pixels: int = 50_000_000
    max_width: int = 20_000
    max_height: int = 20_000
    max_input_bytes: int = 64 * 1024 * 1024
    max_output_bytes: int = 16 * 1024 * 1024

    def __post_init__(self) -> None:
        numeric_positive = (
            self.timeout_seconds,
            self.max_concurrent_operations,
            self.max_image_pixels,
            self.max_width,
            self.max_height,
            self.max_input_bytes,
            self.max_output_bytes,
        )
        if any(value <= 0 for value in numeric_positive):
            raise ValueError("OCR resource limits must be positive")
        if not math.isfinite(self.timeout_seconds):
            raise ValueError("OCR timeout must be finite")
        if self.termination_grace_seconds < 0 or not math.isfinite(
            self.termination_grace_seconds
        ):
            raise ValueError("OCR termination grace must be finite and non-negative")
        if self.max_retries not in {0, 1}:
            raise ValueError("OCR retries must be disabled or limited to one retry")
