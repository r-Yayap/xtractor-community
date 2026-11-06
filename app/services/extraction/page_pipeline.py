from __future__ import annotations

from app.domain.ocr_value import decode_ocr_value
from app.infra.pdf.contracts import PdfBackend, PdfDocumentSession
from app.infra.pdf.models import PdfRect
from app.infra.pdf.text_normalization import character_label
from app.services.extraction.area_extractor import AreaExtractor
from app.services.extraction.contracts import (
    ExtractionPlan,
    PageExtractionContext,
    PageExtractionResult,
    SuppressionAuditEvent,
)
from app.services.extraction.planning import build_page_plan
from app.services.text_output_policy import apply_text_output_policy


class PageExtractionPipeline:
    """Compose one page's final values, audits, and summary facts."""

    def __init__(
        self,
        backend: PdfBackend,
        plan: ExtractionPlan,
        area_extractor: AreaExtractor,
    ) -> None:
        self._backend = backend
        self._plan = plan
        self._area_extractor = area_extractor

    def extract(
        self,
        document: PdfDocumentSession,
        page_number: int,
        context: PageExtractionContext,
    ) -> PageExtractionResult:
        page_info = self._backend.documents.page_info(document, page_number)
        page_plan = build_page_plan(self._plan, page_info)
        audits: list[SuppressionAuditEvent] = []

        def output_value(
            value: object,
            *,
            area_index: int,
            area_title: str,
            rect: tuple[float, float, float, float] | None,
        ) -> str:
            result = apply_text_output_policy(value, self._plan.text_output)
            if result.excluded and self._plan.text_output.audit_excluded_text:
                audit_rect = (
                    PdfRect.from_value(rect) if rect else PdfRect(0, 0, 0, 0)
                )
                audits.extend(
                    SuppressionAuditEvent(
                        area_index=area_index,
                        area_title=area_title,
                        value=excluded.value,
                        rect=audit_rect,
                        reason=f"Excluded {excluded.classification} text by setting",
                    )
                    for excluded in result.excluded
                )
            return result.value

        extracted = self._area_extractor.extract_page(
            document,
            page_plan,
            image_target=context.image_target,
        )
        area_values: list[str] = []
        duplicate_text_removed = False
        for item in extracted:
            area = item.area.area
            for occurrence in item.suppressed:
                duplicate_text_removed = True
                audits.append(
                    SuppressionAuditEvent(
                        area_index=area.index,
                        area_title=area.title,
                        value=occurrence.text,
                        rect=occurrence.rect,
                        font=occurrence.font,
                        font_size=occurrence.font_size,
                        kept_sequence=occurrence.kept_sequence,
                        suppressed_sequence=occurrence.suppressed_sequence,
                        reason="Duplicate overlay",
                    )
                )
            for normalization in item.normalizations:
                audits.append(
                    SuppressionAuditEvent(
                        area_index=area.index,
                        area_title=area.title,
                        value=character_label(normalization.source),
                        rect=PdfRect.from_value(item.area.source_rect),
                        reason="Character normalization",
                        replacement=character_label(normalization.replacement),
                        count=normalization.count,
                    )
                )
            area_values.append(
                output_value(
                    item.value,
                    area_index=area.index,
                    area_title=area.title,
                    rect=item.area.source_rect,
                )
                if item.value.strip()
                else ""
            )

        blank_reference = False
        reference_index = self._plan.reference_column_index
        if (
            isinstance(reference_index, int)
            and not isinstance(reference_index, bool)
            and 0 <= reference_index < len(area_values)
        ):
            reference_text, _ = decode_ocr_value(area_values[reference_index])
            blank_reference = not str(reference_text).strip()

        return PageExtractionResult(
            page_plan=page_plan,
            area_values=tuple(area_values),
            audit_events=tuple(audits),
            blank_reference=blank_reference,
            different_page_size=page_plan.different_page_size,
            duplicate_text_removed=duplicate_text_removed,
        )
