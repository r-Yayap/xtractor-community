from __future__ import annotations

from pathlib import Path

import pypdfium2 as pdfium

from app.infra.pdf.errors import (
    BackendInternalError,
    EncryptedPdfError,
    InvalidPdfError,
)


def normalize_name(value: str | Path | None) -> str:
    return "memory.pdf" if value is None else str(value)


def map_load_error(exc: pdfium.PdfiumError, *, name: str) -> Exception:
    if exc.err_code in {
        pdfium.raw.FPDF_ERR_PASSWORD,
        pdfium.raw.FPDF_ERR_SECURITY,
    }:
        return EncryptedPdfError(f"Could not open encrypted PDF: {name}")
    return InvalidPdfError(f"Could not open PDF: {name}")


def require_session(document):
    from app.infra.pdf.pdfium.document_backend import PdfiumDocumentSession

    if not isinstance(document, PdfiumDocumentSession):
        raise BackendInternalError(
            "The active PDFium backend received a document from another backend"
        )
    document.ensure_open()
    return document
