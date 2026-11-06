from __future__ import annotations

from app.infra.pdf.contracts import PdfBackend


def create_pdf_backend() -> PdfBackend:
    from app.infra.pdf.pdfium import create_pdfium_backend

    return create_pdfium_backend()


def initialize_pdf_backend() -> None:
    create_pdf_backend().maintenance.release_caches()
