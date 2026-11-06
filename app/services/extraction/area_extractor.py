from __future__ import annotations

import gc
import logging
from pathlib import Path
from time import perf_counter
from typing import Callable

from app.infra.pdf.contracts import PdfBackend, PdfDocumentSession
from app.infra.pdf.errors import OcrError
from app.infra.pdf.models import OcrResult, TextExtractionResult
from app.services.extraction.contracts import (
    AreaExtractionResult,
    ExtractionDiagnostic,
    PageExtractionPlan,
)
from app.services.extraction.native_text_quality import (
    is_high_confidence_ocr,
    looks_malformed,
)


ImageTarget = Callable[[int], Path]


class AreaExtractor:
    """Extract all configured areas for one page while containing area failures."""

    def __init__(
        self,
        backend: PdfBackend,
        *,
        dpi: int,
        scale: float | None,
        ocr_enabled: bool,
        logger: logging.Logger | None = None,
    ) -> None:
        self._backend = backend
        self._dpi = max(1, int(dpi))
        self._scale = scale
        self._ocr_enabled = ocr_enabled
        self._logger = logger or logging.getLogger(__name__)
        self._render_count = 0

    def extract_page(
        self,
        document: PdfDocumentSession,
        page: PageExtractionPlan,
        *,
        image_target: ImageTarget | None,
    ) -> tuple[AreaExtractionResult, ...]:
        started = perf_counter()
        native_started = started
        native_indexes = tuple(
            area.area.index for area in page.areas if not area.area.use_ocr
        )
        native_by_index: dict[int, TextExtractionResult] = {}
        native_errors: dict[int, Exception] = {}
        if native_indexes:
            try:
                values = self._backend.text.extract_regions(
                    document,
                    page.page_number,
                    tuple(page.areas[index].text_rect for index in native_indexes),
                )
                if len(values) != len(native_indexes):
                    raise ValueError(
                        "Native batch extraction returned an unexpected result count"
                    )
                native_by_index = dict(zip(native_indexes, values))
            except Exception as batch_error:
                self._logger.debug(
                    "Native batch extraction failed for page %s; retrying %s areas individually: %s",
                    page.page_number + 1,
                    len(native_indexes),
                    type(batch_error).__name__,
                )
                for index in native_indexes:
                    try:
                        native_by_index[index] = self._backend.text.extract_region(
                            document,
                            page.page_number,
                            page.areas[index].text_rect,
                        )
                    except Exception as exc:
                        native_errors[index] = exc
        native_ms = (perf_counter() - native_started) * 1000.0

        automatic_malformed_indexes = frozenset(
            item.area.index
            for item in page.areas
            if self._ocr_enabled
            and self._backend.identity is not None
            and self._backend.identity.name == "pdfium"
            and not item.area.use_ocr
            and item.area.index in native_by_index
            and looks_malformed(native_by_index[item.area.index].text)
        )

        ocr_indexes = tuple(
            item.area.index
            for item in page.areas
            if item.image_rect is not None
            and (
                item.area.use_ocr
                or (
                    self._ocr_enabled
                    and item.area.index not in native_errors
                    and item.area.index in native_by_index
                    and not native_by_index[item.area.index].text.strip()
                )
                or item.area.index in automatic_malformed_indexes
            )
        )
        ocr_by_index: dict[int, OcrResult] = {}
        ocr_errors: dict[int, Exception] = {}
        batch_ocr = bool(
            self._backend.identity is not None
            and self._backend.identity.name == "pdfium"
        )
        if ocr_indexes and batch_ocr:
            ocr_started = perf_counter()
            try:
                values = self._backend.ocr.extract_regions(
                    document,
                    page.page_number,
                    tuple(page.areas[index].image_rect for index in ocr_indexes),  # type: ignore[arg-type]
                    dpi=self._dpi,
                    scale=self._scale,
                )
                if len(values) != len(ocr_indexes):
                    raise ValueError(
                        "OCR batch extraction returned an unexpected result count"
                    )
                ocr_by_index = dict(zip(ocr_indexes, values))
            except OcrError as batch_error:
                ocr_errors = {index: batch_error for index in ocr_indexes}
            except Exception as batch_error:
                self._logger.debug(
                    "OCR batch extraction failed for page %s; retrying %s areas individually: %s",
                    page.page_number + 1,
                    len(ocr_indexes),
                    type(batch_error).__name__,
                )
                for index in ocr_indexes:
                    try:
                        ocr_by_index[index] = self._extract_ocr(
                            document, page, index
                        )
                    except Exception as exc:
                        ocr_errors[index] = exc
            ocr_ms = (perf_counter() - ocr_started) * 1000.0
        else:
            ocr_ms = 0.0

        results: list[AreaExtractionResult] = []
        for item in page.areas:
            index = item.area.index
            requested_ocr = item.area.use_ocr
            staged_image: Path | None = None
            try:
                if item.area.use_image and item.image_rect is not None:
                    if image_target is None:
                        raise RuntimeError("image target was unavailable")
                    staged_image = image_target(index)
                    image = self._backend.render.render_region(
                        document,
                        page.page_number,
                        item.image_rect,
                        dpi=self._dpi,
                        scale=self._scale,
                    )
                    staged_image.write_bytes(image.data)
                    self._render_count += 1
                    if self._render_count % 20 == 0:
                        gc.collect()
                    if staged_image.stat().st_size > 30 * 1024 * 1024:
                        staged_image.unlink(missing_ok=True)
                        staged_image = None

                if requested_ocr:
                    if item.image_rect is None:
                        results.append(
                            AreaExtractionResult(
                                item,
                                "OCR_ERROR",
                                "error",
                                ExtractionDiagnostic(
                                    "invalid_ocr_clip",
                                    "OCR area did not intersect the page",
                                ),
                                staged_image=staged_image,
                            )
                        )
                        continue
                    if index in ocr_errors:
                        raise ocr_errors[index]
                    value = ocr_by_index.get(index) or self._extract_ocr(
                        document, page, index
                    )
                    results.append(
                        AreaExtractionResult(
                            item,
                            value.text,
                            "forced_ocr",
                            staged_image=staged_image,
                        )
                    )
                    continue

                if index in native_errors:
                    raise native_errors[index]
                native = native_by_index[index]
                if index in automatic_malformed_indexes:
                    try:
                        if index in ocr_errors:
                            raise ocr_errors[index]
                        value = ocr_by_index.get(index) or self._extract_ocr(
                            document, page, index
                        )
                    except Exception:
                        value = None
                    if value is not None and is_high_confidence_ocr(value):
                        results.append(
                            AreaExtractionResult(
                                item,
                                value.text,
                                "ocr_malformed_fallback",
                                staged_image=staged_image,
                            )
                        )
                        continue
                if self._ocr_enabled and not native.text.strip() and item.image_rect:
                    try:
                        if index in ocr_errors:
                            raise ocr_errors[index]
                        value = ocr_by_index.get(index) or self._extract_ocr(
                            document, page, index
                        )
                        results.append(
                            AreaExtractionResult(
                                item,
                                value.text,
                                "ocr_fallback",
                                staged_image=staged_image,
                            )
                        )
                    except Exception as exc:
                        results.append(
                            AreaExtractionResult(
                                item,
                                "OCR_ERROR",
                                "error",
                                ExtractionDiagnostic("ocr_failed", type(exc).__name__),
                                staged_image=staged_image,
                            )
                        )
                    continue
                results.append(
                    AreaExtractionResult(
                        item,
                        native.text,
                        "native",
                        suppressed=native.suppressed,
                        normalizations=native.normalizations,
                        staged_image=staged_image,
                    )
                )
            except Exception as exc:
                self._logger.debug(
                    "Area extraction returned an empty value for page %s area %s: %s",
                    page.page_number + 1,
                    index,
                    type(exc).__name__,
                )
                results.append(
                    AreaExtractionResult(
                        item,
                        "OCR_ERROR" if requested_ocr else "",
                        "error",
                        ExtractionDiagnostic(
                            "ocr_failed" if requested_ocr else (
                                "native_failed" if index in native_errors else "area_failed"
                            ),
                            type(exc).__name__,
                        ),
                        staged_image=staged_image,
                    )
                )
        self._logger.debug(
            "Area extraction page %s: native=%.1fms, OCR batch=%.1fms, total=%.1fms "
            "(%s native, %s OCR)",
            page.page_number + 1,
            native_ms,
            ocr_ms,
            (perf_counter() - started) * 1000.0,
            len(native_indexes),
            len(ocr_indexes),
        )
        return tuple(results)

    def _extract_ocr(
        self,
        document: PdfDocumentSession,
        page: PageExtractionPlan,
        index: int,
    ) -> OcrResult:
        rect = page.areas[index].image_rect
        if rect is None:
            raise ValueError("OCR area did not intersect the page")
        started = perf_counter()
        try:
            return self._backend.ocr.extract_region(
                document,
                page.page_number,
                rect,
                dpi=self._dpi,
                scale=self._scale,
            )
        finally:
            self._logger.debug(
                "OCR extraction page %s area %s completed in %.1fms",
                page.page_number + 1,
                index,
                (perf_counter() - started) * 1000.0,
            )
