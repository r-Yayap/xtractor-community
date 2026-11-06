from __future__ import annotations

from app.infra.ocr.manager import OcrManager
from app.infra.ocr.models import OcrPolicy
from app.infra.ocr.tesseract.engine import TesseractEngine
from app.infra.ocr.tesseract.runtime import discover_runtime


def default_ocr_policy(*, timeout_seconds: float = 30.0) -> OcrPolicy:
    return OcrPolicy(timeout_seconds=timeout_seconds)


def create_ocr_manager(
    *,
    policy: OcrPolicy | None = None,
) -> OcrManager:
    selected_policy = policy or default_ocr_policy()
    runtime = discover_runtime()
    return OcrManager(TesseractEngine(runtime, selected_policy), selected_policy)
