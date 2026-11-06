from __future__ import annotations

from collections.abc import Iterable

from app.domain.extraction_summary import ExtractionSummary, PdfExtractionFacts


class SummaryCollector:
    @staticmethod
    def collect(
        *,
        successful_facts: Iterable[PdfExtractionFacts],
        error_count: int,
        duration_hms: str,
        reference_column_index: int | None,
        reference_size_available: bool,
    ) -> ExtractionSummary:
        facts = tuple(successful_facts)
        return ExtractionSummary(
            processed_pdfs=len(facts) + error_count,
            duration_hms=duration_hms,
            different_page_sizes=(
                sum(item.different_page_size for item in facts)
                if reference_size_available
                else None
            ),
            blank_reference_number=(
                sum(item.blank_reference_cells for item in facts)
                if reference_column_index is not None
                else None
            ),
            duplicate_text_removed=sum(
                item.duplicate_text_removed for item in facts
            ),
            multi_page_pdfs=sum(item.pages > 1 for item in facts),
            errors=error_count,
            reference_column_detected=reference_column_index is not None,
            reference_size_available=reference_size_available,
        )
