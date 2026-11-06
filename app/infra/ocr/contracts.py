from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from app.infra.ocr.models import OcrRequest, OcrResult


@runtime_checkable
class OcrEngine(Protocol):
    """The complete public OCR engine contract."""

    def recognize(
        self,
        request: OcrRequest,
        *,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> OcrResult: ...
