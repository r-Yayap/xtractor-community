from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import re

import pypdfium2 as pdfium

from app.infra.pdf.contracts import PdfDocumentSession
from app.infra.pdf.errors import (
    BackendInternalError,
    EmptyDocumentError,
    InvalidPageError,
    PdfFileNotFoundError,
)
from app.infra.pdf.models import PdfPageInfo
from app.infra.pdf.pdfium.common import map_load_error, normalize_name, require_session
from app.infra.pdf.pdfium.coordinates import PdfiumCoordinateSpace


class PdfiumDocumentSession:
    def __init__(
        self,
        document: pdfium.PdfDocument,
        *,
        name: str,
        source_bytes: bytes | None,
        normalize_rotation: bool,
    ) -> None:
        self._document = document
        self._name = name
        self._source_bytes = source_bytes
        self._normalize_rotation = normalize_rotation
        self._pages: dict[int, object] = {}
        self._closed = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def page_count(self) -> int:
        self.ensure_open()
        return len(self._document)

    @property
    def needs_password(self) -> bool:
        self.ensure_open()
        return False

    @contextmanager
    def open_page(self, page_number: int):
        self.ensure_open()
        if not isinstance(page_number, int) or not 0 <= page_number < len(self._document):
            raise InvalidPageError(
                f"Page {page_number!r} is outside document range 0..{len(self._document) - 1}"
            )
        page = self._pages.get(page_number)
        if page is None:
            page = self._document.get_page(page_number)
            self._pages[page_number] = page
        yield page

    def ensure_open(self) -> None:
        if self._closed:
            raise BackendInternalError(f"PDF document is already closed: {self._name}")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            for page in self._pages.values():
                page.close()
            self._pages.clear()
        finally:
            self._document.close()
            self._source_bytes = None

    def __enter__(self) -> "PdfiumDocumentSession":
        self.ensure_open()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


class PdfiumDocumentCapability:
    def open_path(self, path: Path, *, normalize_rotation: bool) -> PdfDocumentSession:
        source = Path(path)
        if not source.is_file():
            compatibility_error = FileNotFoundError(f"no such file: '{source}'")
            raise PdfFileNotFoundError(str(compatibility_error)) from compatibility_error
        return self._open(
            str(source),
            name=str(source),
            source_bytes=None,
            normalize_rotation=normalize_rotation,
        )

    def open_bytes(
        self,
        data: bytes,
        *,
        name: str | None = None,
        normalize_rotation: bool = False,
    ) -> PdfDocumentSession:
        payload = bytes(data)
        return self._open(
            payload,
            name=normalize_name(name),
            source_bytes=payload,
            normalize_rotation=normalize_rotation,
        )

    def page_info(self, document: PdfDocumentSession, page_number: int) -> PdfPageInfo:
        session = require_session(document)
        with session.open_page(page_number) as page:
            space = PdfiumCoordinateSpace.from_page(page)
            source_rotation = space.rotation_degrees
        normalized = bool(session._normalize_rotation and source_rotation)
        return PdfPageInfo(
            page_number=page_number,
            bounds=space.app_bounds,
            media_box=space.media_box,
            crop_box=space.crop_box,
            source_rotation_degrees=source_rotation,
            rotation_degrees=0 if session._normalize_rotation else source_rotation,
            normalization_applied=normalized,
        )

    @staticmethod
    def _open(
        source,
        *,
        name: str,
        source_bytes: bytes | None,
        normalize_rotation: bool,
    ) -> PdfiumDocumentSession:
        try:
            document = pdfium.PdfDocument(source)
        except pdfium.PdfiumError as exc:
            if _looks_like_zero_page_pdf(source):
                raise EmptyDocumentError(f"PDF contains no pages: {name}") from exc
            raise map_load_error(exc, name=name) from exc
        try:
            if len(document) == 0:
                raise EmptyDocumentError(f"PDF contains no pages: {name}")
            return PdfiumDocumentSession(
                document,
                name=name,
                source_bytes=source_bytes,
                normalize_rotation=normalize_rotation,
            )
        except Exception:
            document.close()
            raise


def _looks_like_zero_page_pdf(source) -> bool:
    try:
        payload = source if isinstance(source, bytes) else Path(source).read_bytes()
    except (OSError, TypeError, ValueError):
        return False
    return bool(
        re.search(rb"/Type\s*/Pages\b", payload)
        and re.search(rb"/Count\s+0\b", payload)
    )
