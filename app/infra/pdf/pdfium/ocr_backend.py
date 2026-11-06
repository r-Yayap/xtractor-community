from __future__ import annotations

import hashlib
import threading

from app.infra.ocr.composition import create_ocr_manager, default_ocr_policy
from app.infra.ocr.errors import OcrSubsystemError
from app.infra.ocr.grouping import OcrRegion, assign_words_to_regions, plan_region_groups
from app.infra.ocr.models import (
    OcrDiagnostic,
    OcrDiagnosticCode,
    OcrImage,
    OcrRect,
    OcrRequest,
    OcrResult as NeutralOcrResult,
    OcrSegmentationMode,
    OcrTransform,
)
from app.infra.pdf.contracts import PdfDocumentSession
from app.infra.pdf.errors import InvalidClipError, OcrError
from app.infra.pdf.models import (
    DiagnosticCode,
    ExtractionDiagnostic,
    OcrResult,
    OcrWord,
    PdfRect,
)
from app.infra.pdf.pdfium.common import require_session
from app.infra.pdf.pdfium.render_backend import PdfiumRenderCapability


class PdfiumOcrCapability:
    """Temporary PDF capability adapter over the independent OCR subsystem."""

    def __init__(
        self,
        render: PdfiumRenderCapability,
        *,
        timeout_seconds: float = 30.0,
        grouping_enabled: bool = False,
        grouping_gap_points: float = 18.0,
    ) -> None:
        self._render = render
        self._policy = default_ocr_policy(timeout_seconds=timeout_seconds)
        self._grouping_enabled = grouping_enabled
        self._grouping_gap_points = grouping_gap_points
        self._manager_instance: object | None = None
        self._manager_lock = threading.Lock()

    def _manager(self):
        with self._manager_lock:
            if self._manager_instance is None:
                self._manager_instance = create_ocr_manager(policy=self._policy)
            return self._manager_instance

    @staticmethod
    def _source_id(session) -> str:
        return hashlib.sha256(
            session.name.encode("utf-8", errors="replace")
        ).hexdigest()

    def _recognize(
        self,
        session,
        page_number: int,
        effective: PdfRect,
        *,
        dpi: int,
        scale: float | None,
        area_id: str,
        segmentation_mode: OcrSegmentationMode = OcrSegmentationMode.AUTO,
    ) -> NeutralOcrResult:
        planned = plan_region_groups(
            (OcrRegion(area_id, _neutral_rect(effective)),),
            dpi=dpi,
            scale=scale,
            max_pixels=self._policy.max_image_pixels,
            max_width=self._policy.max_width,
            max_height=self._policy.max_height,
            max_gap_points=0,
        )[0]
        if (
            planned.pixel_width > self._policy.max_width
            or planned.pixel_height > self._policy.max_height
            or planned.pixel_count > self._policy.max_image_pixels
        ):
            diagnostic = OcrDiagnostic(
                OcrDiagnosticCode.INPUT_TOO_LARGE,
                "OCR render region exceeded its configured pixel policy",
            )
            raise OcrError("OCR render region was too large", diagnostics=(diagnostic,))
        try:
            image = self._render.render_region(
                session,
                page_number,
                effective,
                dpi=dpi,
                scale=scale,
            )
            source_id = self._source_id(session)
            request = OcrRequest(
                image=OcrImage(bytes(image.data), image.width, image.height),
                transform=OcrTransform(
                    scale_x=effective.width / image.width,
                    scale_y=effective.height / image.height,
                    offset_x=effective.x0,
                    offset_y=effective.y0,
                ),
                operation_id=f"{source_id}:{page_number}:{effective.to_tuple()}",
                source_id=source_id,
                page_number=page_number,
                area_id=area_id,
                segmentation_mode=segmentation_mode,
            )
            result = self._manager().recognize(request)
        except OcrSubsystemError as exc:
            raise OcrError(
                "OCR runtime validation failed",
                diagnostics=(exc.diagnostic,),
            ) from exc
        if not result.succeeded:
            raise OcrError("OCR operation failed", diagnostics=result.diagnostics)
        return result

    def _effective(self, document: PdfDocumentSession, page_number: int, rect: PdfRect):
        session = require_session(document)
        with session.open_page(page_number) as page:
            width, height = page.get_size()
        effective = rect.intersect(PdfRect(0, 0, float(width), float(height)))
        if effective is None:
            raise InvalidClipError(f"Empty or invalid PDF clip: {rect.to_tuple()}")
        return session, effective

    def extract_region(
        self,
        document: PdfDocumentSession,
        page_number: int,
        rect: PdfRect,
        *,
        dpi: int,
        scale: float | None,
    ) -> OcrResult:
        session, effective = self._effective(document, page_number, rect)
        area_id = hashlib.sha256(
            repr(effective.to_tuple()).encode("ascii")
        ).hexdigest()
        result = self._recognize(
            session,
            page_number,
            effective,
            dpi=dpi,
            scale=scale,
            area_id=area_id,
        )
        if (
            not result.words
            and not (result.text or "").strip()
            and min(effective.width, effective.height) <= 32.0
        ):
            for segmentation_mode in (
                OcrSegmentationMode.SINGLE_LINE,
                OcrSegmentationMode.SINGLE_CHARACTER,
            ):
                result = self._recognize(
                    session,
                    page_number,
                    effective,
                    dpi=dpi,
                    scale=scale,
                    area_id=area_id,
                    segmentation_mode=segmentation_mode,
                )
                if result.words or (result.text or "").strip():
                    break
        return _legacy_result(result)

    def extract_regions(
        self,
        document: PdfDocumentSession,
        page_number: int,
        rects: tuple[PdfRect, ...],
        *,
        dpi: int,
        scale: float | None,
    ) -> tuple[OcrResult, ...]:
        if not rects:
            return ()
        session = require_session(document)
        effective_regions: list[tuple[int, PdfRect]] = []
        for index, rect in enumerate(rects):
            _, effective = self._effective(session, page_number, rect)
            effective_regions.append((index, effective))
        if not self._grouping_enabled:
            return tuple(
                self.extract_region(
                    session,
                    page_number,
                    effective,
                    dpi=dpi,
                    scale=scale,
                )
                for _, effective in effective_regions
            )

        groups = plan_region_groups(
            tuple(
                OcrRegion(str(index), _neutral_rect(effective))
                for index, effective in effective_regions
            ),
            dpi=dpi,
            scale=scale,
            max_pixels=self._policy.max_image_pixels,
            max_width=self._policy.max_width,
            max_height=self._policy.max_height,
            max_gap_points=self._grouping_gap_points,
        )
        output: dict[int, OcrResult] = {}
        effective_by_index = dict(effective_regions)
        for group in groups:
            indexes = tuple(int(region.area_id) for region in group.regions)
            if len(indexes) == 1:
                index = indexes[0]
                output[index] = self.extract_region(
                    session,
                    page_number,
                    effective_by_index[index],
                    dpi=dpi,
                    scale=scale,
                )
                continue
            grouped_rect = PdfRect(
                group.bounds.x0,
                group.bounds.y0,
                group.bounds.x1,
                group.bounds.y1,
            )
            try:
                result = self._recognize(
                    session,
                    page_number,
                    grouped_rect,
                    dpi=dpi,
                    scale=scale,
                    area_id="group:" + ",".join(str(index) for index in indexes),
                )
                assignment = assign_words_to_regions(result.words, group.regions)
            except OcrError:
                raise
            except Exception:
                assignment = None
            if assignment is None or assignment.ambiguous:
                fallback_diagnostic = ExtractionDiagnostic(
                    DiagnosticCode.FALLBACK_USED,
                    "Grouped OCR was not safely assignable; individual OCR was used.",
                    page_number=page_number,
                    used_fallback=True,
                )
                for index in indexes:
                    individual = self.extract_region(
                        session,
                        page_number,
                        effective_by_index[index],
                        dpi=dpi,
                        scale=scale,
                    )
                    output[index] = OcrResult(
                        individual.text,
                        individual.diagnostics + (fallback_diagnostic,),
                        individual.words,
                    )
                continue
            for index in indexes:
                area_id = str(index)
                output[index] = _legacy_result(
                    NeutralOcrResult.success(
                        assignment.text_by_area[area_id],
                        words=assignment.words_by_area[area_id],
                        elapsed_ms=result.elapsed_ms,
                        attempts=result.attempts,
                        runtime_version=result.runtime_version,
                    )
                )
        return tuple(output[index] for index in range(len(rects)))


def _neutral_rect(rect: PdfRect) -> OcrRect:
    return OcrRect(rect.x0, rect.y0, rect.x1, rect.y1)


def _legacy_result(result: NeutralOcrResult) -> OcrResult:
    diagnostics = ()
    if result.diagnostics:
        diagnostics = (
            ExtractionDiagnostic(
                DiagnosticCode.FALLBACK_USED,
                "OCR completed after a contained runtime diagnostic",
                used_fallback=True,
                cause_codes=tuple(item.code.value for item in result.diagnostics),
            ),
        )
    accepted_words = tuple(
        word for word in result.words if word.confidence > 0.0
    )
    accepted_text = result.text or ""
    if result.words and len(accepted_words) != len(result.words):
        accepted_text = " ".join(word.text for word in accepted_words)
    return OcrResult(
        text="_OCR_" + accepted_text,
        diagnostics=diagnostics,
        words=tuple(
            OcrWord(
                text=word.text,
                rect=PdfRect(
                    word.rect.x0,
                    word.rect.y0,
                    word.rect.x1,
                    word.rect.y1,
                ),
                confidence=word.confidence,
                block_number=word.block_number,
                paragraph_number=word.paragraph_number,
                line_number=word.line_number,
                word_number=word.word_number,
            )
            for word in accepted_words
        ),
    )
