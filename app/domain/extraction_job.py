from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from app.domain.extraction_outcome import DIAGNOSTIC_TEXT_LIMIT, ExtractionDiagnostic
from app.domain.extraction_progress import (
    ExtractionProgressSnapshot,
    is_progress_regression,
)
from app.domain.extraction_summary import ExtractionResult


class ExtractionJobStatus(str, Enum):
    DRAFT = "draft"
    READY = "ready"
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    FINALIZING = "finalizing"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    NEEDS_ATTENTION = "needs_attention"


class ExtractionJobFailure(str, Enum):
    OUTPUT_MISSING = "output_missing"


class ExtractionJobDiagnosticCode(str, Enum):
    STARTUP_FAILED = "startup_failed"
    PROCESS_FAILURE = "process_failure"
    PROCESS_CRASH = "process_crash"
    FINALIZATION_FAILED = "finalization_failed"
    PROTOCOL_FAILURE = "protocol_failure"
    OUTPUT_MISSING = "output_missing"
    RESTART_INTERRUPTED = "restart_interrupted"


class ExtractionJobTransitionError(ValueError):
    """Raised when a lifecycle change is not allowed by the canonical matrix."""


_ACTIVE_STATUSES = frozenset(
    {
        ExtractionJobStatus.STARTING,
        ExtractionJobStatus.RUNNING,
        ExtractionJobStatus.FINALIZING,
        ExtractionJobStatus.CANCELLING,
    }
)
_TERMINAL_STATUSES = frozenset(
    {
        ExtractionJobStatus.CANCELLED,
        ExtractionJobStatus.COMPLETED,
        ExtractionJobStatus.COMPLETED_WITH_WARNINGS,
        ExtractionJobStatus.FAILED,
    }
)
_RESTART_RECOVERABLE_STATUSES = frozenset(
    {
        ExtractionJobStatus.STARTING,
        ExtractionJobStatus.RUNNING,
        ExtractionJobStatus.FINALIZING,
        ExtractionJobStatus.CANCELLING,
    }
)
def _normalize_diagnostic_message(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("message must be a string")
    normalized = " ".join(value.split())
    if not normalized:
        normalized = "Unknown extraction error."
    return normalized[:DIAGNOSTIC_TEXT_LIMIT]


@dataclass(frozen=True, slots=True)
class ExtractionJobDiagnostic:
    code: ExtractionJobDiagnosticCode
    message: str
    transport: ExtractionDiagnostic | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.code, ExtractionJobDiagnosticCode):
            raise TypeError("code must be an ExtractionJobDiagnosticCode")
        object.__setattr__(self, "message", _normalize_diagnostic_message(self.message))
        if self.transport is not None and not isinstance(
            self.transport, ExtractionDiagnostic
        ):
            raise TypeError("transport must be an ExtractionDiagnostic or None")


@dataclass(frozen=True, slots=True)
class ExtractionJobState:
    job_id: int = 0
    status: ExtractionJobStatus = ExtractionJobStatus.DRAFT
    progress: ExtractionProgressSnapshot = field(
        default_factory=ExtractionProgressSnapshot
    )
    started_at: float | None = None
    elapsed_seconds: float | None = None
    result: ExtractionResult | None = None
    failure: ExtractionJobFailure | None = None
    diagnostic: ExtractionJobDiagnostic | None = None

    def __post_init__(self) -> None:
        if isinstance(self.job_id, bool) or not isinstance(self.job_id, int) or self.job_id < 0:
            raise TypeError("job_id must be a non-negative integer")
        if not isinstance(self.status, ExtractionJobStatus):
            raise TypeError("status must be an ExtractionJobStatus")
        if not isinstance(self.progress, ExtractionProgressSnapshot):
            raise TypeError("progress must be an ExtractionProgressSnapshot")
        if self.result is not None and not isinstance(self.result, ExtractionResult):
            raise TypeError("result must be an ExtractionResult or None")
        if self.failure is not None and not isinstance(self.failure, ExtractionJobFailure):
            raise TypeError("failure must be an ExtractionJobFailure or None")
        if self.diagnostic is not None and not isinstance(
            self.diagnostic, ExtractionJobDiagnostic
        ):
            raise TypeError("diagnostic must be an ExtractionJobDiagnostic or None")

    @property
    def processed_files(self) -> int:
        return self.progress.processed_files

    @property
    def total_files(self) -> int:
        return self.progress.total_files

    @property
    def active(self) -> bool:
        return self.status in _ACTIVE_STATUSES

    @property
    def terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES


ALLOWED_EXTRACTION_JOB_TRANSITIONS: Mapping[
    ExtractionJobStatus, frozenset[ExtractionJobStatus]
] = MappingProxyType(
    {
        ExtractionJobStatus.DRAFT: frozenset({ExtractionJobStatus.READY}),
        ExtractionJobStatus.READY: frozenset(
            {
                ExtractionJobStatus.QUEUED,
                ExtractionJobStatus.STARTING,
                ExtractionJobStatus.NEEDS_ATTENTION,
            }
        ),
        ExtractionJobStatus.QUEUED: frozenset(
            {
                ExtractionJobStatus.STARTING,
                ExtractionJobStatus.CANCELLED,
                ExtractionJobStatus.NEEDS_ATTENTION,
            }
        ),
        ExtractionJobStatus.STARTING: frozenset(
            {ExtractionJobStatus.RUNNING, ExtractionJobStatus.FAILED}
        ),
        ExtractionJobStatus.RUNNING: frozenset(
            {ExtractionJobStatus.CANCELLING, ExtractionJobStatus.FINALIZING}
        ),
        ExtractionJobStatus.FINALIZING: frozenset(
            {
                ExtractionJobStatus.CANCELLED,
                ExtractionJobStatus.COMPLETED,
                ExtractionJobStatus.COMPLETED_WITH_WARNINGS,
                ExtractionJobStatus.FAILED,
            }
        ),
        ExtractionJobStatus.CANCELLING: frozenset({ExtractionJobStatus.FINALIZING}),
        ExtractionJobStatus.CANCELLED: frozenset(),
        ExtractionJobStatus.COMPLETED: frozenset(),
        ExtractionJobStatus.COMPLETED_WITH_WARNINGS: frozenset(),
        ExtractionJobStatus.FAILED: frozenset(),
        ExtractionJobStatus.INTERRUPTED: frozenset(
            {
                ExtractionJobStatus.READY,
                ExtractionJobStatus.QUEUED,
                ExtractionJobStatus.CANCELLED,
                ExtractionJobStatus.NEEDS_ATTENTION,
            }
        ),
        ExtractionJobStatus.NEEDS_ATTENTION: frozenset(
            {
                ExtractionJobStatus.READY,
                ExtractionJobStatus.QUEUED,
                ExtractionJobStatus.CANCELLED,
            }
        ),
    }
)


def transition_job(
    state: ExtractionJobState,
    target: ExtractionJobStatus,
    **updates: object,
) -> ExtractionJobState:
    if not isinstance(state, ExtractionJobState):
        raise TypeError("state must be an ExtractionJobState")
    if not isinstance(target, ExtractionJobStatus):
        raise TypeError("target must be an ExtractionJobStatus")
    if "status" in updates:
        raise TypeError("status updates must use the target argument")
    allowed = ALLOWED_EXTRACTION_JOB_TRANSITIONS[state.status]
    if target not in allowed:
        raise ExtractionJobTransitionError(
            f"Cannot transition extraction job from '{state.status.value}' to '{target.value}'."
        )
    return replace(state, status=target, **updates)


def update_job_progress(
    state: ExtractionJobState,
    *,
    progress: ExtractionProgressSnapshot,
) -> ExtractionJobState:
    if not isinstance(state, ExtractionJobState):
        raise TypeError("state must be an ExtractionJobState")
    if not isinstance(progress, ExtractionProgressSnapshot):
        raise TypeError("progress must be an ExtractionProgressSnapshot")
    if progress == state.progress or is_progress_regression(state.progress, progress):
        return state
    return replace(state, progress=progress)

def completed_status_for_result(result: ExtractionResult) -> ExtractionJobStatus:
    if not isinstance(result, ExtractionResult):
        raise TypeError("result must be an ExtractionResult")
    summary = result.summary
    actionable_counts = (
        summary.different_page_sizes,
        summary.blank_reference_number,
        summary.errors,
    )
    if any((count or 0) > 0 for count in actionable_counts):
        return ExtractionJobStatus.COMPLETED_WITH_WARNINGS
    return ExtractionJobStatus.COMPLETED


def recover_after_restart(state: ExtractionJobState) -> ExtractionJobState:
    if not isinstance(state, ExtractionJobState):
        raise TypeError("state must be an ExtractionJobState")
    if state.status not in _RESTART_RECOVERABLE_STATUSES:
        return state
    return replace(
        state,
        status=ExtractionJobStatus.INTERRUPTED,
        diagnostic=ExtractionJobDiagnostic(
            code=ExtractionJobDiagnosticCode.RESTART_INTERRUPTED,
            message="Application restart interrupted the active extraction job.",
        ),
    )
