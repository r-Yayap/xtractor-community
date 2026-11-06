from __future__ import annotations

from app.infra.ocr.models import OcrDiagnostic


class OcrSubsystemError(RuntimeError):
    """Private control-flow error carrying a safe neutral diagnostic."""

    def __init__(self, diagnostic: OcrDiagnostic) -> None:
        super().__init__(diagnostic.message)
        self.diagnostic = diagnostic
