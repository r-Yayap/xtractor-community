from __future__ import annotations

from enum import Enum


class PdfErrorCode(str, Enum):
    FILE_NOT_FOUND = "file_not_found"
    INVALID_PDF = "invalid_pdf"
    ENCRYPTED_PDF = "encrypted_pdf"
    EMPTY_DOCUMENT = "empty_document"
    INVALID_PAGE = "invalid_page"
    INVALID_CLIP = "invalid_clip"
    TEXT_EXTRACTION_FAILED = "text_extraction_failed"
    RENDERING_FAILED = "rendering_failed"
    OCR_FAILED = "ocr_failed"
    UNSUPPORTED_FEATURE = "unsupported_feature"
    BACKEND_INTERNAL = "backend_internal"


class PdfBackendError(Exception):
    code = PdfErrorCode.BACKEND_INTERNAL


class PdfFileNotFoundError(PdfBackendError):
    code = PdfErrorCode.FILE_NOT_FOUND


class InvalidPdfError(PdfBackendError):
    code = PdfErrorCode.INVALID_PDF


class EncryptedPdfError(PdfBackendError):
    code = PdfErrorCode.ENCRYPTED_PDF


class EmptyDocumentError(PdfBackendError):
    code = PdfErrorCode.EMPTY_DOCUMENT


class InvalidPageError(PdfBackendError):
    code = PdfErrorCode.INVALID_PAGE


class InvalidClipError(PdfBackendError):
    code = PdfErrorCode.INVALID_CLIP


class TextExtractionError(PdfBackendError):
    code = PdfErrorCode.TEXT_EXTRACTION_FAILED


class RenderingError(PdfBackendError):
    code = PdfErrorCode.RENDERING_FAILED


class OcrError(PdfBackendError):
    code = PdfErrorCode.OCR_FAILED

    def __init__(self, message: str, *, diagnostics: tuple[object, ...] = ()) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics


class UnsupportedFeatureError(PdfBackendError):
    code = PdfErrorCode.UNSUPPORTED_FEATURE


class BackendInternalError(PdfBackendError):
    code = PdfErrorCode.BACKEND_INTERNAL
