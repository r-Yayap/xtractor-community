from __future__ import annotations

import logging
import multiprocessing as mp
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.domain.extraction_outcome import (
    ExtractionDiagnostic,
    ExtractionOutcome,
    ExtractionOutcomeCategory,
)
from app.domain.extraction_progress import (
    ExtractionProgressPhase,
    ExtractionProgressSnapshot,
    is_progress_regression,
)
from app.domain.models import ExtractionRequest
from app.services.extraction_service import ExtractionService


logger = logging.getLogger(__name__)


@dataclass
class ExtractionJob:
    process: mp.Process
    cancel_event: mp.Event
    manager: mp.Manager
    progress_json: "mp.managers.ValueProxy[str]"
    final_outcome_json: "mp.managers.ValueProxy[str]"
    last_progress: ExtractionProgressSnapshot
    started_at: float
    progress_transport_failed: bool = False

    @property
    def processed_files(self) -> int:
        return self.last_progress.processed_files

    @property
    def total_files(self) -> int:
        return self.last_progress.total_files


def _run_service(
    req: ExtractionRequest,
    progress_json: "mp.managers.ValueProxy[str]",
    final_outcome_json: "mp.managers.ValueProxy[str]",
    cancel_evt: mp.Event,
) -> None:
    progress_publication_failed = False

    def on_progress(snapshot: ExtractionProgressSnapshot) -> None:
        nonlocal progress_publication_failed
        if progress_publication_failed:
            return
        try:
            progress_json.value = snapshot.to_json()
        except Exception as exc:
            progress_publication_failed = True
            logger.warning(
                "Extraction progress publication failed (%s)",
                type(exc).__name__,
            )

    def should_cancel() -> bool:
        return cancel_evt.is_set()

    try:
        svc = ExtractionService()
        result = svc.extract(
            req,
            on_progress=on_progress,
            should_cancel=should_cancel,
        )
        if result is not None:
            outcome = ExtractionOutcome.success(result)
        elif cancel_evt.is_set():
            outcome = ExtractionOutcome.cancelled()
        else:
            outcome = ExtractionOutcome.failed(
                ExtractionDiagnostic(
                    category=ExtractionOutcomeCategory.UNEXPECTED_NO_RESULT,
                    message="Extraction stopped without producing a result.",
                )
            )
    except Exception as exc:
        outcome = ExtractionOutcome.failed(
            ExtractionDiagnostic.for_exception(
                ExtractionOutcomeCategory.SERVICE_EXCEPTION,
                exc,
                message="Extraction service failed unexpectedly.",
            )
        )
    try:
        final_outcome_json.value = outcome.to_json()
    except Exception as exc:
        # The parent observes a normal exit without a valid envelope as a crash.
        logger.error(
            "Failed to publish extraction terminal outcome (%s)",
            type(exc).__name__,
        )


class ExtractController:
    def start(self, req: ExtractionRequest) -> ExtractionJob:
        cancel_evt = mp.Event()
        manager = mp.Manager()
        initial_progress = ExtractionProgressSnapshot(
            phase=ExtractionProgressPhase.PREPARING,
            total_files=len(req.pdf_paths),
            ocr_enabled=req.ocr.enabled,
        )
        progress_json = manager.Value("s", initial_progress.to_json())
        final_out = manager.Value("s", "")

        p = mp.Process(
            target=_run_service,
            args=(req, progress_json, final_out, cancel_evt),
            daemon=False
        )
        p.start()

        return ExtractionJob(
            process=p,
            cancel_event=cancel_evt,
            manager=manager,
            progress_json=progress_json,
            final_outcome_json=final_out,
            last_progress=initial_progress,
            started_at=time.time(),
        )

    def poll(self, job: ExtractionJob) -> Optional[ExtractionProgressSnapshot]:
        if not job.process.is_alive():
            return None
        try:
            payload = job.progress_json.value
            candidate = ExtractionProgressSnapshot.from_json(payload)
        except Exception as exc:
            if not job.progress_transport_failed:
                job.progress_transport_failed = True
                logger.warning(
                    "Extraction progress transport failed (%s)",
                    type(exc).__name__,
                )
            return job.last_progress
        if is_progress_regression(job.last_progress, candidate):
            return job.last_progress
        if candidate != job.last_progress:
            job.last_progress = candidate
        return job.last_progress

    def finish(self, job: ExtractionJob) -> ExtractionOutcome:
        exit_code: int | None = None
        payload: object = ""
        join_error: Exception | None = None
        exit_code_error: Exception | None = None
        payload_error: Exception | None = None
        try:
            try:
                job.process.join(timeout=2)
            except Exception as exc:
                join_error = exc
            try:
                exit_code = job.process.exitcode
            except Exception as exc:
                exit_code_error = exc
            try:
                payload = job.final_outcome_json.value
            except Exception as exc:
                payload_error = exc
            return _classify_terminal_outcome(
                exit_code=exit_code,
                payload=payload,
                join_error=join_error,
                exit_code_error=exit_code_error,
                payload_error=payload_error,
            )
        finally:
            try:
                _reap_after_finish(job.process)
            finally:
                try:
                    job.manager.shutdown()
                except Exception as exc:
                    logger.warning(
                        "Extraction manager shutdown failed (%s)",
                        type(exc).__name__,
                    )

    def cancel(self, job: ExtractionJob) -> bool:
        try:
            job.cancel_event.set()
        except Exception as exc:
            logger.warning(
                "Extraction cancellation signal failed (%s)",
                type(exc).__name__,
            )
            return False
        return True

    def dispose(self, job: ExtractionJob) -> None:
        """Cancel and boundedly release every process resource owned by a job."""
        self.cancel(job)
        process = job.process
        cleanup_errors: list[tuple[str, Exception]] = []
        try:
            process.join(timeout=2)
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
            if process.is_alive():
                process.kill()
                process.join(timeout=2)
        except Exception as exc:
            cleanup_errors.append(("process disposal", exc))

        try:
            process.close()
        except Exception as exc:
            cleanup_errors.append(("process close", exc))
        try:
            job.manager.shutdown()
        except Exception as exc:
            cleanup_errors.append(("manager shutdown", exc))

        if cleanup_errors:
            for operation, exception in cleanup_errors[1:]:
                _log_process_cleanup_failure(operation, exception)
            raise cleanup_errors[0][1]


def _crashed_from_exception(
    category: ExtractionOutcomeCategory,
    exception: Exception,
    message: str,
) -> ExtractionOutcome:
    return ExtractionOutcome.crashed(
        ExtractionDiagnostic.for_exception(
            category,
            exception,
            message=message,
        )
    )


def _classify_terminal_outcome(
    *,
    exit_code: int | None,
    payload: object,
    join_error: Exception | None,
    exit_code_error: Exception | None,
    payload_error: Exception | None,
) -> ExtractionOutcome:
    if exit_code_error is not None:
        return _crashed_from_exception(
            ExtractionOutcomeCategory.FINALIZATION_FAILED,
            exit_code_error,
            "Extraction process status could not be finalized.",
        )
    if exit_code is None:
        return ExtractionOutcome.crashed(
            ExtractionDiagnostic(
                category=ExtractionOutcomeCategory.CHILD_PROCESS_EXIT,
                message="Extraction process did not terminate cleanly.",
            )
        )
    if exit_code != 0:
        return ExtractionOutcome.crashed(
            ExtractionDiagnostic(
                category=ExtractionOutcomeCategory.CHILD_PROCESS_EXIT,
                message="Extraction process exited unexpectedly.",
                exit_code=exit_code,
            )
        )
    if join_error is not None:
        return _crashed_from_exception(
            ExtractionOutcomeCategory.FINALIZATION_FAILED,
            join_error,
            "Extraction process could not be finalized.",
        )
    if payload_error is not None:
        return _crashed_from_exception(
            ExtractionOutcomeCategory.PROTOCOL_FAILURE,
            payload_error,
            "Extraction process returned an invalid terminal outcome.",
        )
    if not isinstance(payload, str) or not payload.strip():
        return ExtractionOutcome.crashed(
            ExtractionDiagnostic(
                category=ExtractionOutcomeCategory.MISSING_OUTCOME,
                message="Extraction process ended without reporting a terminal outcome.",
            )
        )
    try:
        return ExtractionOutcome.from_json(payload)
    except Exception:
        return ExtractionOutcome.crashed(
            ExtractionDiagnostic(
                category=ExtractionOutcomeCategory.PROTOCOL_FAILURE,
                message="Extraction process returned an invalid terminal outcome.",
            )
        )


def _reap_after_finish(process: mp.Process) -> None:
    try:
        alive = process.is_alive()
    except Exception as exc:
        _log_process_cleanup_failure("process is_alive check", exc)
        alive = False
    if alive:
        try:
            process.terminate()
        except Exception as exc:
            _log_process_cleanup_failure("process terminate", exc)
        try:
            process.join(timeout=2)
        except Exception as exc:
            _log_process_cleanup_failure("process join", exc)
        try:
            alive = process.is_alive()
        except Exception as exc:
            _log_process_cleanup_failure("process is_alive check", exc)
            alive = False
        if alive:
            try:
                process.kill()
            except Exception as exc:
                _log_process_cleanup_failure("process kill", exc)
            try:
                process.join(timeout=2)
            except Exception as exc:
                _log_process_cleanup_failure("process join", exc)
    try:
        process.close()
    except Exception as exc:
        _log_process_cleanup_failure("process close", exc)


def _log_process_cleanup_failure(operation: str, exception: Exception) -> None:
    logger.warning(
        "Extraction %s cleanup failed (%s)",
        operation,
        type(exception).__name__,
    )
