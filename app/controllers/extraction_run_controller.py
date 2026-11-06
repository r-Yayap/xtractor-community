from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import logging
from pathlib import Path
import time
from typing import Any

from app.controllers.extract_controller import ExtractController
from app.controllers.extraction_resource_admission import (
    ExtractionAdmissionLease,
    ExtractionResourceAdmission,
)
from app.domain.extraction_outcome import (
    ExtractionDiagnostic,
    ExtractionOutcome,
    ExtractionOutcomeStatus,
)
from app.domain.extraction_job import (
    ExtractionJobDiagnostic,
    ExtractionJobDiagnosticCode,
    ExtractionJobFailure,
    ExtractionJobState,
    ExtractionJobStatus,
    completed_status_for_result,
    transition_job,
    update_job_progress,
)
from app.domain.extraction_progress import (
    ExtractionProgressPhase,
    ExtractionProgressSnapshot,
)
from app.domain.models import ExtractionRequest


logger = logging.getLogger(__name__)


class ExtractionAdmissionUnavailable(RuntimeError):
    """Raised when shared extraction capacity cannot admit a new run."""


class ExtractionRunController:
    """Own one extraction process lifecycle above the low-level adapter."""

    def __init__(
        self,
        executor: Any | None = None,
        *,
        admission: ExtractionResourceAdmission,
        clock: Callable[[], float] = time.monotonic,
        output_exists: Callable[[Path], bool] = Path.exists,
    ) -> None:
        self._executor = executor or ExtractController()
        self._admission = admission
        self._clock = clock
        self._output_exists = output_exists
        self._state = ExtractionJobState()
        self._job: Any | None = None
        self._lease: ExtractionAdmissionLease | None = None
        self._poll_warning_logged = False

    @property
    def state(self) -> ExtractionJobState:
        return self._state

    def start(self, request: ExtractionRequest) -> ExtractionJobState:
        if self._state.active:
            raise RuntimeError("An extraction run is already active.")
        candidate = ExtractionJobState(
            job_id=self._state.job_id + 1,
            progress=ExtractionProgressSnapshot(
                phase=ExtractionProgressPhase.PREPARING,
                total_files=len(request.pdf_paths),
                ocr_enabled=request.ocr.enabled,
            ),
        )
        candidate = transition_job(candidate, ExtractionJobStatus.READY)
        lease = self._admission.try_acquire()
        if lease is None:
            raise ExtractionAdmissionUnavailable(
                "Extraction resources are not currently available."
            )
        self._state = transition_job(
            candidate,
            ExtractionJobStatus.STARTING,
            started_at=self._clock(),
        )
        self._poll_warning_logged = False
        try:
            job = self._executor.start(request)
        except Exception as exc:
            self._admission.release(lease)
            self._state = transition_job(
                self._state,
                ExtractionJobStatus.FAILED,
                diagnostic=ExtractionJobDiagnostic(
                    code=ExtractionJobDiagnosticCode.STARTUP_FAILED,
                    message="Extraction process could not be started safely.",
                ),
            )
            logger.warning(
                "Extraction startup failed (%s)",
                type(exc).__name__,
            )
            return self._state
        self._lease = lease
        self._job = job
        self._state = transition_job(self._state, ExtractionJobStatus.RUNNING)
        return self._state

    def poll(self) -> ExtractionJobState:
        if not self._state.active or self._job is None:
            return self._state
        try:
            polled = self._executor.poll(self._job)
        except Exception as exc:
            self._warn_poll_failure_once(exc)
            self._state = self._with_active_elapsed(self._state)
            return self._state
        if polled is not None:
            try:
                if not isinstance(polled, ExtractionProgressSnapshot):
                    raise TypeError(
                        "progress poll must return an ExtractionProgressSnapshot"
                    )
                progress = polled
            except Exception as exc:
                self._warn_poll_failure_once(exc)
                self._state = self._with_active_elapsed(self._state)
                return self._state
            self._state = self._with_active_elapsed(self._state)
            self._state = update_job_progress(self._state, progress=progress)
            return self._state
        return self._finish()

    def _warn_poll_failure_once(self, exc: Exception) -> None:
        if self._poll_warning_logged:
            return
        self._poll_warning_logged = True
        logger.warning(
            "Extraction progress polling failed (%s)",
            type(exc).__name__,
        )

    def request_cancel(self) -> bool:
        if self._state.status is not ExtractionJobStatus.RUNNING or self._job is None:
            return False
        try:
            cancel_result = self._executor.cancel(self._job)
        except Exception as exc:
            logger.warning(
                "Extraction cancellation request failed (%s)",
                type(exc).__name__,
            )
            return False
        if cancel_result is False:
            logger.warning("Extraction cancellation request was not issued")
            return False
        self._state = transition_job(self._state, ExtractionJobStatus.CANCELLING)
        return True

    def dispose(self) -> ExtractionJobState:
        if not self._state.active or self._job is None:
            return self._state
        job = self._job
        self._job = None
        if self._state.status is ExtractionJobStatus.RUNNING:
            self._state = transition_job(self._state, ExtractionJobStatus.CANCELLING)
        elapsed = self._elapsed_seconds()
        self._state = transition_job(
            self._state,
            ExtractionJobStatus.FINALIZING,
            progress=self._state.progress.with_phase(ExtractionProgressPhase.FINALIZING),
            elapsed_seconds=elapsed,
        )
        try:
            self._executor.dispose(job)
        except Exception as exc:
            logger.warning(
                "Extraction resource disposal failed (%s)",
                type(exc).__name__,
            )
            self._state = transition_job(
                self._state,
                ExtractionJobStatus.FAILED,
                diagnostic=self._finalization_diagnostic(
                    exc,
                    message="Extraction resources could not be released cleanly.",
                ),
            )
            return self._state
        self._release_lease()
        self._state = transition_job(self._state, ExtractionJobStatus.CANCELLED)
        return self._state

    def _finish(self) -> ExtractionJobState:
        job = self._job
        self._job = None
        self._state = transition_job(
            self._state,
            ExtractionJobStatus.FINALIZING,
            progress=self._state.progress.with_phase(ExtractionProgressPhase.FINALIZING),
            elapsed_seconds=self._elapsed_seconds(),
        )
        try:
            outcome = self._executor.finish(job)
        except Exception as exc:
            self._state = transition_job(
                self._state,
                ExtractionJobStatus.FAILED,
                diagnostic=self._finalization_diagnostic(
                    exc,
                    message="Extraction process could not be finalized.",
                ),
            )
            return self._state
        self._release_lease()
        if not isinstance(outcome, ExtractionOutcome):
            self._state = transition_job(
                self._state,
                ExtractionJobStatus.FAILED,
                diagnostic=ExtractionJobDiagnostic(
                    code=ExtractionJobDiagnosticCode.PROTOCOL_FAILURE,
                    message="Extraction process returned an invalid terminal outcome.",
                ),
            )
            return self._state
        if outcome.status is ExtractionOutcomeStatus.CANCELLED:
            self._state = transition_job(self._state, ExtractionJobStatus.CANCELLED)
            return self._state
        if outcome.status is ExtractionOutcomeStatus.FAILED:
            self._state = transition_job(
                self._state,
                ExtractionJobStatus.FAILED,
                diagnostic=self._transport_diagnostic(
                    ExtractionJobDiagnosticCode.PROCESS_FAILURE,
                    outcome.diagnostic,
                ),
            )
            return self._state
        if outcome.status is ExtractionOutcomeStatus.CRASHED:
            self._state = transition_job(
                self._state,
                ExtractionJobStatus.FAILED,
                diagnostic=self._transport_diagnostic(
                    ExtractionJobDiagnosticCode.PROCESS_CRASH,
                    outcome.diagnostic,
                ),
            )
            return self._state
        result = outcome.result
        if result is None:
            self._state = transition_job(
                self._state,
                ExtractionJobStatus.FAILED,
                diagnostic=ExtractionJobDiagnostic(
                    code=ExtractionJobDiagnosticCode.PROTOCOL_FAILURE,
                    message="Extraction process returned an invalid terminal outcome.",
                ),
            )
            return self._state
        if not self._output_exists(result.output_path):
            self._state = transition_job(
                self._state,
                ExtractionJobStatus.FAILED,
                result=result,
                failure=ExtractionJobFailure.OUTPUT_MISSING,
                diagnostic=ExtractionJobDiagnostic(
                    code=ExtractionJobDiagnosticCode.OUTPUT_MISSING,
                    message="Extraction finished but no output workbook was created.",
                ),
            )
            return self._state
        self._state = transition_job(
            self._state,
            completed_status_for_result(result),
            result=result,
        )
        return self._state

    def _release_lease(self) -> None:
        lease = self._lease
        if lease is not None and self._admission.release(lease):
            self._lease = None

    def _elapsed_seconds(self) -> float | None:
        started_at = self._state.started_at
        if started_at is None:
            return None
        return max(0.0, self._clock() - started_at)

    def _with_active_elapsed(self, state: ExtractionJobState) -> ExtractionJobState:
        if not state.active:
            return state
        elapsed = self._elapsed_seconds()
        if elapsed is None or state.elapsed_seconds == elapsed:
            return state
        return replace(state, elapsed_seconds=elapsed)

    @staticmethod
    def _transport_diagnostic(
        code: ExtractionJobDiagnosticCode,
        transport: ExtractionDiagnostic | None,
    ) -> ExtractionJobDiagnostic:
        message = (
            transport.message
            if transport is not None
            else "Extraction process failed without a diagnostic message."
        )
        return ExtractionJobDiagnostic(
            code=code,
            message=message,
            transport=transport,
        )

    @staticmethod
    def _finalization_diagnostic(
        _exception: Exception,
        *,
        message: str,
    ) -> ExtractionJobDiagnostic:
        return ExtractionJobDiagnostic(
            code=ExtractionJobDiagnosticCode.FINALIZATION_FAILED,
            message=message,
        )
