from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from app.infra.pdf.models import (
    BackendIdentity,
    OcrResult,
    PdfCharacter,
    PdfPageInfo,
    PdfRect,
    PdfWord,
    RenderedImage,
    TextExtractionResult,
)


@runtime_checkable
class PdfDocumentSession(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def page_count(self) -> int: ...

    @property
    def needs_password(self) -> bool: ...

    def close(self) -> None: ...

    def __enter__(self) -> "PdfDocumentSession": ...

    def __exit__(self, exc_type, exc, traceback) -> None: ...


@runtime_checkable
class DocumentCapability(Protocol):
    def open_path(self, path: Path, *, normalize_rotation: bool) -> PdfDocumentSession: ...

    def open_bytes(
        self,
        data: bytes,
        *,
        name: str | None = None,
        normalize_rotation: bool = False,
    ) -> PdfDocumentSession: ...

    def page_info(self, document: PdfDocumentSession, page_number: int) -> PdfPageInfo: ...


@runtime_checkable
class RenderCapability(Protocol):
    def render_page(
        self,
        document: PdfDocumentSession,
        page_number: int,
        *,
        scale: float,
    ) -> RenderedImage: ...

    def render_region(
        self,
        document: PdfDocumentSession,
        page_number: int,
        rect: PdfRect,
        *,
        dpi: int,
        scale: float | None,
    ) -> RenderedImage: ...


@runtime_checkable
class TextCapability(Protocol):
    def extract_region(
        self,
        document: PdfDocumentSession,
        page_number: int,
        rect: PdfRect,
    ) -> TextExtractionResult: ...

    def extract_regions(
        self,
        document: PdfDocumentSession,
        page_number: int,
        rects: tuple[PdfRect, ...],
    ) -> tuple[TextExtractionResult, ...]: ...

    def extract_words(
        self,
        document: PdfDocumentSession,
        page_number: int,
        rect: PdfRect,
    ) -> tuple[PdfWord, ...]: ...

    def extract_characters(
        self,
        document: PdfDocumentSession,
        page_number: int,
        rect: PdfRect,
    ) -> tuple[PdfCharacter, ...]: ...


@runtime_checkable
class OcrCapability(Protocol):
    def extract_region(
        self,
        document: PdfDocumentSession,
        page_number: int,
        rect: PdfRect,
        *,
        dpi: int,
        scale: float | None,
    ) -> OcrResult: ...

    def extract_regions(
        self,
        document: PdfDocumentSession,
        page_number: int,
        rects: tuple[PdfRect, ...],
        *,
        dpi: int,
        scale: float | None,
    ) -> tuple[OcrResult, ...]: ...


@runtime_checkable
class MaintenanceCapability(Protocol):
    def release_caches(self) -> None: ...


@dataclass(frozen=True, slots=True)
class PdfBackend:
    documents: DocumentCapability
    render: RenderCapability
    text: TextCapability
    ocr: OcrCapability
    maintenance: MaintenanceCapability
    identity: BackendIdentity | None = None
