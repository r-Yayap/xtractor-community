"""Engine-neutral OCR boundary owned by Xtractor."""

from app.infra.ocr.contracts import OcrEngine
from app.infra.ocr.models import (
    OcrDiagnostic,
    OcrDiagnosticCode,
    OcrImage,
    OcrOutcome,
    OcrPolicy,
    OcrRect,
    OcrRequest,
    OcrResult,
    OcrTransform,
    OcrWord,
)

__all__ = [
    "OcrDiagnostic",
    "OcrDiagnosticCode",
    "OcrEngine",
    "OcrImage",
    "OcrOutcome",
    "OcrPolicy",
    "OcrRect",
    "OcrRequest",
    "OcrResult",
    "OcrTransform",
    "OcrWord",
]
