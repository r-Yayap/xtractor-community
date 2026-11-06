from __future__ import annotations

import gc
import logging
import os
import sys
import time
from pathlib import Path
from typing import Callable, Mapping

from app.domain.extraction_schema import build_staged_row
from app.domain.extraction_summary import PdfExtractionFacts
from app.infra.file_metadata import (
    FileFingerprint,
    format_metadata_warning,
    fingerprint_file,
    source_changed_since,
)
from app.infra.excel_writer import staged_area_image_name
from app.infra.pdf import create_pdf_backend
from app.infra.pdf.contracts import PdfBackend, PdfDocumentSession
from app.infra.pdf.errors import EmptyDocumentError, PdfBackendError
from app.services.extraction.area_extractor import AreaExtractor
from app.services.extraction.contracts import (
    ExtractionPlan,
    PageExtractionContext,
    PdfProcessor,
)
from app.services.extraction.page_pipeline import PageExtractionPipeline
from app.services.extraction.staging import WorkerStagingWriter


logger = logging.getLogger(__name__)


_SAFE_WORKER_MESSAGES = {
    ("ValueError", "PDF is encrypted or requires a password"),
    ("ValueError", "PDF contains no readable pages"),
    ("FileDataError", "Failed to open as type 'pdf'"),
}


def _safe_worker_error(exception: BaseException) -> str:
    """Return an artifact-safe worker error without arbitrary exception text."""
    compatibility_error = (
        exception.__cause__
        if isinstance(exception, PdfBackendError) and exception.__cause__
        else exception
    )
    exception_type = type(compatibility_error).__name__
    if isinstance(compatibility_error, PermissionError):
        reason = "permission denied"
    elif isinstance(compatibility_error, FileNotFoundError):
        reason = "file not found"
    elif isinstance(compatibility_error, IsADirectoryError):
        reason = "path is a directory"
    elif isinstance(compatibility_error, NotADirectoryError):
        reason = "path is unavailable"
    elif isinstance(compatibility_error, TimeoutError):
        reason = "operation timed out"
    elif isinstance(compatibility_error, OSError):
        reason = "I/O operation failed"
    elif (exception_type, str(compatibility_error)) in _SAFE_WORKER_MESSAGES:
        reason = str(compatibility_error)
    else:
        reason = "extraction failed"
    return f"{exception_type}: {reason}"


def open_pdf_for_extraction(
    backend: PdfBackend,
    pdf_path: Path,
) -> PdfDocumentSession:
    try:
        return backend.documents.open_path(pdf_path, normalize_rotation=True)
    except EmptyDocumentError:
        raise ValueError("PDF contains no readable pages") from None


def process_pdf(
    pdf_path: Path,
    plan: ExtractionPlan,
    temp_dir: Path,
    prefix: str,
    *,
    facts_out: dict[str, object] | None = None,
    backend_factory: Callable[[], PdfBackend] = create_pdf_backend,
    document_opener: Callable[[PdfBackend, Path], PdfDocumentSession] = open_pdf_for_extraction,
    operation_logger: logging.Logger = logger,
    file_fingerprint: FileFingerprint | None = None,
) -> int:
    pdf_path = Path(pdf_path)
    temp_dir = Path(temp_dir)
    backend = backend_factory()
    area_extractor = AreaExtractor(
        backend,
        dpi=plan.ocr_dpi,
        scale=plan.ocr_scale,
        ocr_enabled=plan.ocr_enabled,
        logger=operation_logger,
    )
    pipeline = PageExtractionPipeline(
        backend,
        plan,
        area_extractor,
    )
    staging = WorkerStagingWriter(temp_dir, prefix)
    pages_written = 0
    different_page_size = False
    blank_reference_cells = 0
    duplicate_text_removed = False
    fingerprint = file_fingerprint or fingerprint_file(pdf_path)

    try:
        size = fingerprint.size_bytes
        last_modified = fingerprint.modified_display
        sha256 = fingerprint.sha256
        folder = _relative_folder(pdf_path, Path(plan.pdf_root))
        filename = pdf_path.name

        with document_opener(backend, pdf_path) as document:
            if document.needs_password:
                raise ValueError("PDF is encrypted or requires a password")
            if document.page_count < 1:
                raise ValueError("PDF contains no readable pages")
            for page_number in range(document.page_count):
                unid = f"{prefix}-{page_number + 1}"
                result = pipeline.extract(
                    document,
                    page_number,
                    PageExtractionContext(
                        image_target=lambda index: temp_dir
                        / staged_area_image_name(
                            prefix,
                            filename,
                            page_number + 1,
                            index,
                        ),
                    ),
                )
                different_page_size |= result.different_page_size
                blank_reference_cells += int(result.blank_reference)
                duplicate_text_removed |= result.duplicate_text_removed

                for event in result.audit_events:
                    staging.write_suppression(
                        [
                            unid,
                            folder,
                            filename,
                            page_number + 1,
                            result.page_plan.source_rotation_degrees,
                            event.area_index,
                            event.area_title,
                            event.value,
                            event.rect.x0,
                            event.rect.y0,
                            event.rect.x1,
                            event.rect.y1,
                            event.font,
                            event.font_size,
                            event.kept_sequence,
                            event.suppressed_sequence,
                            event.reason,
                            event.replacement,
                            event.count,
                        ]
                    )
                staging.write_row(
                    build_staged_row(
                        unid=unid,
                        sha256=sha256,
                        size_bytes=size,
                        modified_display=last_modified,
                        folder=folder,
                        filename=filename,
                        page_no=page_number + 1,
                        page_size=result.page_size,
                        error="",
                        area_values=result.area_values,
                    )
                )
                pages_written += 1
                if (page_number + 1) % 10 == 0:
                    staging.flush_primary()
                    gc.collect()
                    backend.maintenance.release_caches()
    finally:
        staging.close(primary_exception_active=sys.exc_info()[0] is not None)

    pages_result = pages_written if pages_written > 0 else 1
    if facts_out is not None:
        facts_out.update(
            PdfExtractionFacts(
                pages=pages_result,
                different_page_size=different_page_size,
                blank_reference_cells=blank_reference_cells,
                duplicate_text_removed=duplicate_text_removed,
            ).to_dict()
        )
    return pages_result


def process_pdf_job(
    args,
    *,
    backend_factory: Callable[[], PdfBackend] = create_pdf_backend,
    document_opener: Callable[[PdfBackend, Path], PdfDocumentSession] = open_pdf_for_extraction,
    processor: PdfProcessor | None = None,
    operation_logger: logging.Logger = logger,
) -> dict[str, object]:
    job_args = tuple(args)
    if len(job_args) < 4:
        raise ValueError("Extraction worker job requires path, plan, temp dir, and prefix")
    pdf_path, raw_plan, temp_dir, prefix = job_args[:4]
    pdf_path = Path(pdf_path)
    temp_dir = Path(temp_dir)
    started = time.perf_counter()
    extraction_started: float | None = None
    extraction_duration_ms = 0.0
    supplied_fingerprint = None
    if len(job_args) > 4:
        if isinstance(job_args[4], FileFingerprint):
            supplied_fingerprint = job_args[4]
        elif isinstance(job_args[4], Mapping):
            supplied_fingerprint = FileFingerprint.from_dict(job_args[4])
    try:
        file_fingerprint = supplied_fingerprint or fingerprint_file(pdf_path)
    except Exception as exc:
        operation_logger.debug(
            "Source fingerprint fallback failed (%s)",
            type(exc).__name__,
        )
        file_fingerprint = FileFingerprint(
            warning=format_metadata_warning("SHA256 unavailable", exc)
        )

    def manifest_metadata() -> dict[str, object]:
        changed, change_warning = source_changed_since(pdf_path, file_fingerprint)
        warnings = [
            item for item in (file_fingerprint.warning, change_warning) if item
        ]
        return {
            "file_metadata": file_fingerprint.to_dict(),
            "source_changed_during_extraction": bool(
                file_fingerprint.source_changed_during_fingerprint or changed
            ),
            "metadata_warning": "; ".join(dict.fromkeys(warnings)),
        }

    plan: ExtractionPlan | None = None
    try:
        facts: dict[str, object] = {}
        if processor is not None:
            extraction_started = time.perf_counter()
            plan = (
                raw_plan
                if isinstance(raw_plan, ExtractionPlan)
                else ExtractionPlan.from_mapping(raw_plan)
            )
            pages = processor(
                pdf_path,
                plan,
                temp_dir,
                prefix,
                facts_out=facts,
                file_fingerprint=file_fingerprint,
            )
        else:
            plan = (
                raw_plan
                if isinstance(raw_plan, ExtractionPlan)
                else ExtractionPlan.from_mapping(raw_plan)
            )
            extraction_started = time.perf_counter()
            pages = process_pdf(
                pdf_path,
                plan,
                temp_dir,
                prefix,
                facts_out=facts,
                backend_factory=backend_factory,
                document_opener=document_opener,
                operation_logger=operation_logger,
                file_fingerprint=file_fingerprint,
            )
        if extraction_started is not None:
            extraction_duration_ms = (
                time.perf_counter() - extraction_started
            ) * 1000
        _publish_audits(temp_dir, prefix)
        return {
            "ok": True,
            "pages": int(pages),
            "facts": facts,
            "prefix": str(prefix),
            "duration_ms": (time.perf_counter() - started) * 1000,
            "extraction_duration_ms": extraction_duration_ms,
            **manifest_metadata(),
        }
    except Exception as exc:
        if extraction_started is not None:
            extraction_duration_ms = (
                time.perf_counter() - extraction_started
            ) * 1000
        _discard_audits(temp_dir, prefix)
        error = _safe_worker_error(exc)
        try:
            error_plan = plan.to_mapping() if plan is not None else raw_plan
            _write_error_row(
                pdf_path,
                error_plan,
                temp_dir,
                prefix,
                error,
                file_fingerprint=file_fingerprint,
            )
            wrote_error_row = True
        except Exception as exc:
            operation_logger.warning(
                "Worker error-row persistence failed for job %s (%s)",
                prefix,
                type(exc).__name__,
            )
            wrote_error_row = False
        return {
            "ok": False,
            "error": error,
            "wrote_error_row": wrote_error_row,
            "prefix": str(prefix),
            "duration_ms": (time.perf_counter() - started) * 1000,
            "extraction_duration_ms": extraction_duration_ms,
            **manifest_metadata(),
        }


def _relative_folder(pdf_path: Path, root: Path) -> str:
    try:
        return os.path.relpath(pdf_path.parent, root)
    except ValueError:
        return str(pdf_path.parent)


def _write_error_row(
    pdf_path: Path,
    plan: Mapping[str, object],
    temp_dir: Path,
    prefix: str,
    error: str,
    *,
    file_fingerprint: FileFingerprint | None = None,
) -> None:
    staging = WorkerStagingWriter(temp_dir, prefix)
    try:
        fingerprint = file_fingerprint or FileFingerprint()
        staging.write_row(
            build_staged_row(
                unid=f"{prefix}-ERR",
                sha256=fingerprint.sha256,
                size_bytes=fingerprint.size_bytes,
                modified_display=fingerprint.modified_display,
                folder=_relative_folder(pdf_path, Path(str(plan["pdf_root"]))),
                filename=pdf_path.name,
                page_no="",
                page_size="",
                error=error,
                area_values=[""] * len(plan["areas_rects"]),
            )
        )
    finally:
        staging.close(primary_exception_active=sys.exc_info()[0] is not None)


def _publish_audits(temp_dir: Path, prefix: str) -> None:
    staging = temp_dir / f".suppressed_{prefix}.csv.tmp"
    if staging.is_file():
        staging.replace(temp_dir / f"suppressed_{prefix}.csv")


def _discard_audits(temp_dir: Path, prefix: str) -> None:
    for path in (
        temp_dir / f".suppressed_{prefix}.csv.tmp",
    ):
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.debug(
                "Could not remove non-publishable audit staging for failed worker %s (%s)",
                prefix,
                type(exc).__name__,
            )
