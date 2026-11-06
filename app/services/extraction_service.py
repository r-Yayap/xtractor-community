from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable

from app.domain.extraction_progress import ExtractionProgressSnapshot
from app.domain.extraction_summary import ExtractionResult
from app.domain.models import AreaSpec, ExtractionRequest
from app.infra.file_metadata import FileFingerprint
from app.infra.ocr.concurrency import CrashSafeFileGate
from app.infra.ocr.manager import initialize_ocr_worker
from app.infra.pdf import create_pdf_backend
from app.infra.pdf.contracts import PdfBackend, PdfDocumentSession
from app.infra.pdf.errors import EmptyDocumentError
from app.infra.pdf.text_normalization import normalize_pdf_text
from app.services.extraction.contracts import ExtractionPlan
from app.services.extraction.coordinator import BatchExtractionCoordinator
from app.services.extraction.output import ExtractionOutputWriter
from app.services.extraction.planning import effective_area_options
from app.services.extraction.worker import (
    process_pdf as _run_pdf_job,
    process_pdf_job as _run_pdf_job_safe,
)


logger = logging.getLogger(__name__)


def _format_duration_hms(duration_seconds: float) -> str:
    total_seconds = int(max(0.0, duration_seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _combine_suppression_files(temp_dir: Path, combined_csv: Path) -> int:
    """Compatibility delegate for audit-focused tests and integrations."""
    return ExtractionOutputWriter.combine_suppression_audits(
        temp_dir, combined_csv
    )


def _clean_text(text: str) -> str:
    return normalize_pdf_text(text) if text else ""


def _effective_area_options(
    mode: str,
    area: AreaSpec,
    ocr_enabled: bool = True,
) -> tuple[bool, bool]:
    return effective_area_options(mode, area, ocr_enabled)


def _open_pdf_for_extraction(
    pdf_backend: PdfBackend,
    pdf_path: Path,
) -> PdfDocumentSession:
    try:
        return pdf_backend.documents.open_path(pdf_path, normalize_rotation=True)
    except EmptyDocumentError:
        raise ValueError("PDF contains no readable pages") from None


def _process_single_pdf_plan(
    pdf_path: Path,
    plan: ExtractionPlan,
    temp_dir: Path,
    prefix: str,
    *,
    facts_out: dict[str, object] | None = None,
    file_fingerprint: FileFingerprint | None = None,
) -> int:
    return _run_pdf_job(
        pdf_path,
        plan,
        temp_dir,
        prefix,
        facts_out=facts_out,
        backend_factory=create_pdf_backend,
        document_opener=_open_pdf_for_extraction,
        operation_logger=logger,
        file_fingerprint=file_fingerprint,
    )


def _process_single_pdf(
    pdf_path: Path,
    req: dict,
    temp_dir: Path,
    unid_prefix: str,
    facts_out: dict[str, object] | None = None,
    file_fingerprint: FileFingerprint | None = None,
) -> int:
    """Compatibility entry point for benchmarks, tests, and external callers."""
    return _process_single_pdf_plan(
        pdf_path,
        ExtractionPlan.from_mapping(req),
        temp_dir,
        unid_prefix,
        facts_out=facts_out,
        file_fingerprint=file_fingerprint,
    )


def _process_single_pdf_star(args):
    """Spawn-safe wrapper that returns primitive values and never raises."""
    return _run_pdf_job_safe(
        args,
        backend_factory=create_pdf_backend,
        document_opener=_open_pdf_for_extraction,
        processor=_process_single_pdf_plan,
        operation_logger=logger,
    )


class ExtractionService:
    """Compatibility facade over the batch extraction coordinator."""

    def __init__(self) -> None:
        self._pdf_backend = create_pdf_backend()

    def extract(
        self,
        req: ExtractionRequest,
        on_progress: Callable[[ExtractionProgressSnapshot], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> ExtractionResult | None:
        coordinator = BatchExtractionCoordinator(
            backend=self._pdf_backend,
            worker_entry=_process_single_pdf_star,
            gate_factory=CrashSafeFileGate.create,
            worker_initializer=initialize_ocr_worker,
            temp_base=Path(__file__).parent / "temp",
            cpu_count_provider=os.cpu_count,
        )
        return coordinator.extract(req, on_progress, should_cancel)

    def _combine_temp_files(
        self,
        temp_dir: Path,
        combined_csv: Path,
        unique_headers: dict[int, str],
    ) -> None:
        """Combine staged rows for source diagnostics."""
        ExtractionOutputWriter.combine_worker_rows(
            temp_dir, combined_csv, unique_headers
        )
