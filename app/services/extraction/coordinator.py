from __future__ import annotations

import gc
import logging
import multiprocessing as mp
import os
import secrets
import shutil
import threading
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from app.domain.file_manifest import FileManifestEntry, source_absolute_path
from app.domain.extraction_progress import (
    ExtractionProgressPhase,
    ExtractionProgressSnapshot,
)
from app.domain.extraction_summary import (
    ExtractionResult,
    PdfExtractionFacts,
)
from app.domain.models import AreaSpec, ExtractionRequest
from app.domain.extraction_options import resolve_worker_count
from app.infra.ocr.concurrency import CrashSafeFileGate
from app.infra.ocr.manager import initialize_ocr_worker
from app.infra.ocr.models import OcrPolicy
from app.infra.pdf.contracts import PdfBackend
from app.services.extraction.output import ExtractionOutputWriter
from app.services.extraction.planning import (
    build_extraction_plan,
    effective_area_options,
)
from app.services.extraction.pool_teardown import (
    DEFAULT_POOL_TEARDOWN_POLICY,
    PoolTeardownPolicy,
    PoolTeardownReason,
    WorkerPoolTeardownError,
    teardown_worker_pool,
)
from app.services.extraction.summary import SummaryCollector
from app.services.extraction.worker import process_pdf_job
from app.services.file_manifest_comparison import classify_current_batch
from app.infra.file_metadata import FileFingerprint


logger = logging.getLogger(__name__)


def _cleanup_temp_dir(temp_dir: Path) -> None:
    try:
        shutil.rmtree(temp_dir)
    except FileNotFoundError:
        if not temp_dir.exists():
            return
        logger.error(
            "Failed to clean extraction temporary directory (%s)",
            "FileNotFoundError",
        )
    except Exception as exc:
        logger.error(
            "Failed to clean extraction temporary directory (%s)",
            type(exc).__name__,
        )


def _write_error_report(path: Path, errors: Iterable[str]) -> None:
    """Create the optional error sidecar without replacing an older report."""
    with path.open("x", encoding="utf-8") as stream:
        stream.write("\n".join(errors))


class BatchExtractionCoordinator:
    """Own batch execution, cancellation and final publication."""

    def __init__(
        self,
        *,
        backend: PdfBackend,
        worker_entry=process_pdf_job,
        context_factory=mp.get_context,
        gate_factory=CrashSafeFileGate.create,
        worker_initializer=initialize_ocr_worker,
        temp_base: Path | None = None,
        cpu_count_provider=os.cpu_count,
        pool_teardown_policy: PoolTeardownPolicy = DEFAULT_POOL_TEARDOWN_POLICY,
    ) -> None:
        self._backend = backend
        self._worker_entry = worker_entry
        self._context_factory = context_factory
        self._gate_factory = gate_factory
        self._worker_initializer = worker_initializer
        self._temp_base = temp_base or Path(__file__).parent.parent / "temp"
        self._cpu_count_provider = cpu_count_provider
        self._pool_teardown_policy = pool_teardown_policy

    def extract(
        self,
        request: ExtractionRequest,
        on_progress: Callable[[ExtractionProgressSnapshot], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> ExtractionResult | None:
        started_at = time.time()
        started_at_dt = datetime.now()
        self._temp_base.mkdir(exist_ok=True, parents=True)
        temp_dir = self._temp_base / secrets.token_hex(8)
        temp_dir.mkdir(exist_ok=True, parents=True)

        try:
            pdf_paths = [Path(path) for path in request.pdf_paths]
            total_files = len(pdf_paths)
            pdf_root = (
                Path(request.pdf_root)
                if request.pdf_root
                else _infer_pdf_root(pdf_paths)
            )
            unique_headers = _unique_headers(request.areas)
            extraction_plan = replace(
                build_extraction_plan(request),
                pdf_root=str(pdf_root),
            )
            reference_column_index = extraction_plan.reference_column_index
            reference_size_available = (
                extraction_plan.reference_page_width is not None
                and extraction_plan.reference_page_height is not None
            )
            batch_size = int(os.getenv("PDF_BATCH_SIZE", "500"))
            worker_count = resolve_worker_count(
                request.worker_profile,
                request.custom_worker_count,
                cpu_count=self._cpu_count_provider(),
            )
            worker_count = min(worker_count, max(1, total_files))
            worker_request = extraction_plan.to_mapping()
            jobs = [
                (path, worker_request, temp_dir, str(10000 + index))
                for index, path in enumerate(pdf_paths)
            ]
            path_by_prefix = {str(job[3]): Path(job[0]) for job in jobs}
            errors: list[str] = []
            error_rows_written = 0
            facts: list[PdfExtractionFacts] = []
            durations: dict[str, float] = {}
            manifest_by_prefix: dict[str, FileManifestEntry] = {}
            cancelled = False
            progress = ExtractionProgressSnapshot(
                phase=ExtractionProgressPhase.PREPARING,
                total_files=total_files,
                ocr_enabled=request.ocr.enabled,
            )
            progress_callback_failed = False

            def publish_progress(
                snapshot: ExtractionProgressSnapshot,
            ) -> None:
                nonlocal progress_callback_failed
                if on_progress is None or progress_callback_failed:
                    return
                try:
                    on_progress(snapshot)
                except Exception as exc:
                    progress_callback_failed = True
                    logger.warning(
                        "Extraction progress publication failed (%s)",
                        type(exc).__name__,
                    )

            publish_progress(progress)

            pool = None
            ocr_cancel_event = None
            watcher_stop = threading.Event()
            watcher = None
            watcher_started = False
            watcher_error_lock = threading.Lock()
            watcher_errors: list[BaseException] = []
            primary_error: BaseException | None = None
            primary_error_from_pool_try = False
            ocr_cancel_cleanup_attempted = False

            def watcher_error() -> BaseException | None:
                with watcher_error_lock:
                    return watcher_errors[0] if watcher_errors else None

            def signal_ocr_cancellation() -> None:
                """Best-effort OCR cancellation that cannot become primary."""

                if ocr_cancel_event is None:
                    return
                try:
                    ocr_cancel_event.set()
                except Exception as error:
                    logger.warning(
                        "Extraction OCR cancellation event cleanup failed (%s)",
                        type(error).__name__,
                    )

            try:
                context = self._context_factory("spawn")
                ocr_gate = self._gate_factory(
                    temp_dir / "ocr-admission",
                    OcrPolicy().max_concurrent_operations,
                )
                ocr_cancel_event = context.Event()
                pool = context.Pool(
                    processes=worker_count,
                    maxtasksperchild=50,
                    initializer=self._worker_initializer,
                    initargs=(ocr_gate, ocr_cancel_event),
                )
                progress = progress.with_phase(ExtractionProgressPhase.EXTRACTING)
                publish_progress(progress)
                if should_cancel is not None:

                    def watch_for_cancellation() -> None:
                        try:
                            while not watcher_stop.wait(0.05):
                                if should_cancel():
                                    signal_ocr_cancellation()
                                    return
                        except BaseException as error:
                            # This is a cross-thread handoff: the owner thread
                            # observes and re-raises the watcher throwable.
                            with watcher_error_lock:
                                watcher_errors.append(error)

                    watcher = threading.Thread(
                        target=watch_for_cancellation,
                        name="extraction-cancellation",
                        daemon=True,
                    )
                    watcher.start()
                    watcher_started = True
                for batch in _chunked(jobs, batch_size):
                    if cancelled:
                        break
                    pending = pool.imap_unordered(
                        self._worker_entry, batch, chunksize=1
                    )
                    while True:
                        try:
                            result = pending.next(timeout=0.1)
                        except mp.TimeoutError:
                            if error := watcher_error():
                                raise error
                            if should_cancel and should_cancel():
                                signal_ocr_cancellation()
                                cancelled = True
                                break
                            continue
                        except StopIteration:
                            break
                        prefix = str(result.get("prefix", ""))
                        duration_ms = float(result.get("duration_ms", 0.0))
                        durations[prefix] = duration_ms
                        manifest_by_prefix[prefix] = _manifest_entry_from_result(
                            pdf_path=path_by_prefix.get(prefix, Path(prefix)),
                            pdf_root=pdf_root,
                            prefix=prefix,
                            result=result,
                            duration_ms=duration_ms,
                        )
                        succeeded = bool(result.get("ok"))
                        if succeeded:
                            facts.append(
                                PdfExtractionFacts.from_dict(
                                    result.get("facts", {})
                                )
                            )
                        else:
                            errors.append(
                                str(result.get("error", "Unknown worker error"))
                            )
                            if result.get("wrote_error_row"):
                                error_rows_written += 1
                        progress = progress.record_file(
                            succeeded=succeeded,
                            pages=int(result.get("pages", 0) or 0)
                            if succeeded
                            else 0,
                            recent_file=path_by_prefix.get(
                                prefix, Path(prefix)
                            ).name,
                        )
                        publish_progress(progress)
                        if error := watcher_error():
                            raise error
                        if should_cancel and should_cancel():
                            cancelled = True
                            break
                    if cancelled:
                        break
                    self._backend.maintenance.release_caches()
                    gc.collect()
                if not cancelled:
                    if error := watcher_error():
                        raise error
                    progress = progress.with_phase(ExtractionProgressPhase.COMBINING)
                    publish_progress(progress)
            except BaseException as error:
                # Preserve the authoritative coordinator throwable unchanged;
                # routine cleanup catches only ordinary Exception below.
                primary_error = error
                primary_error_from_pool_try = True
                raise
            finally:
                watcher_stop.set()
                if primary_error is None:
                    primary_error = watcher_error()
                if ocr_cancel_event is not None and (
                    primary_error is not None or cancelled
                ):
                    ocr_cancel_cleanup_attempted = True
                    signal_ocr_cancellation()
                if watcher is not None and watcher_started:
                    try:
                        watcher.join(
                            timeout=self._pool_teardown_policy.watcher_join_timeout_seconds
                        )
                        if watcher.is_alive():
                            logger.warning(
                                "Extraction cancellation watcher remained alive after bounded join"
                            )
                    except Exception as error:
                        logger.warning(
                            "Extraction cancellation watcher cleanup failed (%s)",
                            type(error).__name__,
                        )
                if primary_error is None:
                    primary_error = watcher_error()
                if (
                    ocr_cancel_event is not None
                    and primary_error is not None
                    and not ocr_cancel_cleanup_attempted
                ):
                    ocr_cancel_cleanup_attempted = True
                    signal_ocr_cancellation()
                if pool is not None:
                    reason = (
                        PoolTeardownReason.EXCEPTION
                        if primary_error is not None
                        else (
                            PoolTeardownReason.CANCELLED
                            if cancelled
                            else PoolTeardownReason.SUCCESS
                        )
                    )
                    try:
                        report = teardown_worker_pool(
                            pool,
                            reason=reason,
                            policy=self._pool_teardown_policy,
                        )
                        for diagnostic in report.cleanup_diagnostics:
                            logger.warning(
                                "Extraction worker pool teardown diagnostic: %s",
                                diagnostic,
                            )
                    except WorkerPoolTeardownError as error:
                        for diagnostic in error.report.cleanup_diagnostics:
                            logger.warning(
                                "Extraction worker pool teardown diagnostic: %s",
                                diagnostic,
                            )
                        if primary_error is not None:
                            logger.warning(
                                "Extraction worker pool teardown reported cleanup uncertainty (%s)",
                                type(error).__name__,
                            )
                            if hasattr(primary_error, "add_note"):
                                primary_error.add_note(
                                    "Worker-pool teardown also reported cleanup uncertainty."
                                )
                        else:
                            raise
                if primary_error is not None and not primary_error_from_pool_try:
                    raise primary_error

            if cancelled:
                return None
            file_manifest = classify_current_batch(
                [
                    manifest_by_prefix.get(prefix)
                    or _manifest_entry_from_result(
                        pdf_path=Path(path),
                        pdf_root=pdf_root,
                        prefix=prefix,
                        result={
                            "ok": False,
                            "error": "Worker result missing",
                            "duration_ms": durations.get(prefix, 0.0),
                        },
                        duration_ms=durations.get(prefix, 0.0),
                    )
                    for path, _, _, prefix in jobs
                ]
            )

            combined_csv = temp_dir / "streamed_output.csv"
            ExtractionOutputWriter.combine_worker_rows(
                temp_dir, combined_csv, unique_headers
            )
            suppressed_csv = temp_dir / "suppressed_output.csv"
            suppressed_count = ExtractionOutputWriter.combine_suppression_audits(
                temp_dir, suppressed_csv
            )
            duration_seconds = max(0.0, time.time() - started_at)
            duration_hms = _format_duration_hms(duration_seconds)
            error_count = progress.failed_files
            success_count = progress.succeeded_files
            total_pages_extracted = progress.processed_pages or 0
            summary = SummaryCollector.collect(
                successful_facts=facts,
                error_count=error_count,
                duration_hms=duration_hms,
                reference_column_index=reference_column_index,
                reference_size_available=reference_size_available,
            )
            metrics = [
                ("Start Time", started_at_dt.strftime("%Y-%m-%d %H:%M:%S")),
                ("End Time", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                ("Duration (seconds)", round(duration_seconds, 2)),
                ("Duration (HH:MM:SS)", duration_hms),
                ("Total PDFs", total_files),
                ("Successful PDFs", success_count),
                ("PDFs with Errors", error_count),
                ("Error Rows Written", error_rows_written),
                ("Total Pages Extracted", total_pages_extracted),
                ("OCR Mode", request.ocr.mode),
            ]
            metrics.append(("Suppressed Text Occurrences", suppressed_count))
            metrics.extend(
                (label, int(value) if value.isdigit() else value)
                for label, value in summary.display_rows()
            )
            progress = progress.with_phase(ExtractionProgressPhase.WRITING_EXCEL)
            publish_progress(progress)
            output = ExtractionOutputWriter.write(
                combined_csv=combined_csv,
                output_excel=request.output_excel,
                output_plan=request.output_plan,
                temp_dir=temp_dir,
                unique_headers=unique_headers,
                needs_images=_needs_images(request.ocr.mode, request.areas),
                pdf_root=pdf_root,
                metrics=metrics,
                suppressed_csv=suppressed_csv,
                file_manifest=file_manifest,
                base_order=request.output_base_column_order,
            )
            progress = progress.with_phase(ExtractionProgressPhase.FINALIZING)
            publish_progress(progress)
            if errors:
                try:
                    _write_error_report(
                        output.with_suffix(".errors.txt"),
                        errors,
                    )
                except Exception as exc:
                    logger.warning(
                        "Could not write extraction error report (%s)",
                        type(exc).__name__,
                    )
            return ExtractionResult(output_path=output, summary=summary)
        finally:
            _cleanup_temp_dir(temp_dir)


def _chunked(sequence, size):
    for index in range(0, len(sequence), size):
        yield sequence[index : index + size]


def _infer_pdf_root(paths: Iterable[Path]) -> Path:
    parents = [Path(path).parent for path in paths]
    if not parents:
        return Path.cwd()
    try:
        return Path(os.path.commonpath([str(path) for path in parents]))
    except ValueError:
        return parents[0]


def _unique_headers(areas: Iterable[AreaSpec]) -> dict[int, str]:
    unique: dict[int, str] = {}
    counts: dict[str, int] = {}
    for index, area in enumerate(areas):
        title = area.title or f"Area {index + 1}"
        counts[title] = counts.get(title, 0) + 1
        unique[index] = (
            title if counts[title] == 1 else f"{title} ({counts[title]})"
        )
    return unique


def _needs_images(mode: str, areas: Iterable[AreaSpec]) -> bool:
    return any(effective_area_options(mode, area)[1] for area in areas)


def _format_duration_hms(seconds: float) -> str:
    total = int(max(0.0, seconds))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _manifest_entry_from_result(
    *,
    pdf_path: Path,
    pdf_root: Path,
    prefix: str,
    result: dict[str, object],
    duration_ms: float,
) -> FileManifestEntry:
    fingerprint = FileFingerprint.from_dict(
        result.get("file_metadata")
        if isinstance(result.get("file_metadata"), dict)
        else None
    )
    try:
        folder = os.path.relpath(pdf_path.parent, pdf_root)
        relative_path = os.path.relpath(pdf_path, pdf_root)
    except ValueError:
        folder = str(pdf_path.parent)
        relative_path = str(pdf_path)
    warning = str(result.get("metadata_warning") or fingerprint.warning)
    return FileManifestEntry(
        file_id=str(prefix),
        sha256=fingerprint.sha256,
        size_bytes=fingerprint.size_bytes,
        modified_display=fingerprint.modified_display,
        modified_time_ns=fingerprint.modified_time_ns,
        folder=folder,
        filename=pdf_path.name,
        relative_path=Path(relative_path).as_posix(),
        absolute_path=source_absolute_path(pdf_path),
        duration_ms=max(0.0, float(duration_ms)),
        status="Success" if bool(result.get("ok")) else "Error",
        extraction_error=str(result.get("error") or ""),
        source_changed_during_extraction=bool(
            result.get("source_changed_during_extraction", False)
        ),
        warning_error=warning,
    )
