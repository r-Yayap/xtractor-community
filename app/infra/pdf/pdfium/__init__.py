from __future__ import annotations

import os

import pypdfium2 as pdfium

from app.infra.pdf.contracts import PdfBackend
from app.infra.pdf.models import BackendIdentity
from app.infra.pdf.pdfium.document_backend import PdfiumDocumentCapability
from app.infra.pdf.pdfium.maintenance import PdfiumMaintenanceCapability
from app.infra.pdf.pdfium.ocr_backend import PdfiumOcrCapability
from app.infra.pdf.pdfium.render_backend import PdfiumRenderCapability
from app.infra.pdf.pdfium.text_backend import PdfiumTextCapability


def create_pdfium_backend() -> PdfBackend:
    render = PdfiumRenderCapability()
    text = PdfiumTextCapability()
    ocr = PdfiumOcrCapability(
        render,
        grouping_enabled=os.getenv("XTRACTOR_PDFIUM_OCR_GROUPING", "1") != "0",
    )
    return PdfBackend(
        documents=PdfiumDocumentCapability(),
        render=render,
        text=text,
        ocr=ocr,
        maintenance=PdfiumMaintenanceCapability(),
        identity=BackendIdentity(
            name="pdfium",
            version=str(pdfium.version.PYPDFIUM_INFO),
            engine_version=str(pdfium.version.PDFIUM_INFO.build),
        ),
    )


__all__ = ["create_pdfium_backend"]
