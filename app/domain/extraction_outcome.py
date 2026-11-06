from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from enum import Enum
import json

from app.domain.extraction_summary import ExtractionResult, ExtractionSummary


EXTRACTION_OUTCOME_SCHEMA_VERSION = 1
DIAGNOSTIC_TEXT_LIMIT = 240
_RESULT_FIELDS = {"output_path", "summary"}
_SUMMARY_FIELDS = {field.name for field in fields(ExtractionSummary)}


class ExtractionOutcomeStatus(str, Enum):
    SUCCESS = "success"
    CANCELLED = "cancelled"
    FAILED = "failed"
    CRASHED = "crashed"


class ExtractionOutcomeCategory(str, Enum):
    SERVICE_EXCEPTION = "service_exception"
    UNEXPECTED_NO_RESULT = "unexpected_no_result"
    CHILD_PROCESS_EXIT = "child_process_exit"
    MISSING_OUTCOME = "missing_outcome"
    PROTOCOL_FAILURE = "protocol_failure"
    FINALIZATION_FAILED = "finalization_failed"


@dataclass(frozen=True, slots=True)
class ExtractionDiagnostic:
    category: ExtractionOutcomeCategory
    message: str
    error_type: str | None = None
    exit_code: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.category, ExtractionOutcomeCategory):
            raise TypeError("category must be an ExtractionOutcomeCategory")
        _validate_text(self.message, "message")
        if self.error_type is not None:
            _validate_text(self.error_type, "error_type")
        if self.exit_code is not None and (
            isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int)
        ):
            raise TypeError("exit_code must be an integer or None")

    @classmethod
    def for_exception(
        cls,
        category: ExtractionOutcomeCategory,
        exception: Exception,
        *,
        message: str,
    ) -> ExtractionDiagnostic:
        return cls(
            category=category,
            message=_bounded_text(message),
            error_type=_bounded_text(type(exception).__name__),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "category": self.category.value,
            "message": self.message,
            "error_type": self.error_type,
            "exit_code": self.exit_code,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ExtractionDiagnostic:
        expected = {"category", "message", "error_type", "exit_code"}
        if set(payload) != expected:
            raise ValueError("diagnostic fields were invalid")
        try:
            category = ExtractionOutcomeCategory(payload["category"])
        except (TypeError, ValueError) as exc:
            raise ValueError("diagnostic category was invalid") from exc
        return cls(
            category=category,
            message=payload["message"],  # type: ignore[arg-type]
            error_type=payload["error_type"],  # type: ignore[arg-type]
            exit_code=payload["exit_code"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class ExtractionOutcome:
    status: ExtractionOutcomeStatus
    result: ExtractionResult | None = None
    diagnostic: ExtractionDiagnostic | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, ExtractionOutcomeStatus):
            raise TypeError("status must be an ExtractionOutcomeStatus")
        if self.status is ExtractionOutcomeStatus.SUCCESS:
            if not isinstance(self.result, ExtractionResult):
                raise ValueError("successful outcomes require a result")
            if self.diagnostic is not None:
                raise ValueError("successful outcomes cannot contain a diagnostic")
            return
        if self.result is not None:
            raise ValueError("non-success outcomes cannot contain a result")
        if self.status is ExtractionOutcomeStatus.CANCELLED:
            if self.diagnostic is not None:
                raise ValueError("cancelled outcomes cannot contain a diagnostic")
            return
        if not isinstance(self.diagnostic, ExtractionDiagnostic):
            raise ValueError("failed and crashed outcomes require a diagnostic")

    @classmethod
    def success(cls, result: ExtractionResult) -> ExtractionOutcome:
        return cls(ExtractionOutcomeStatus.SUCCESS, result=result)

    @classmethod
    def cancelled(cls) -> ExtractionOutcome:
        return cls(ExtractionOutcomeStatus.CANCELLED)

    @classmethod
    def failed(cls, diagnostic: ExtractionDiagnostic) -> ExtractionOutcome:
        return cls(ExtractionOutcomeStatus.FAILED, diagnostic=diagnostic)

    @classmethod
    def crashed(cls, diagnostic: ExtractionDiagnostic) -> ExtractionOutcome:
        return cls(ExtractionOutcomeStatus.CRASHED, diagnostic=diagnostic)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": EXTRACTION_OUTCOME_SCHEMA_VERSION,
            "status": self.status.value,
            "result": self.result.to_dict() if self.result is not None else None,
            "diagnostic": (
                self.diagnostic.to_dict() if self.diagnostic is not None else None
            ),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"))

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ExtractionOutcome:
        expected = {"schema_version", "status", "result", "diagnostic"}
        if set(payload) != expected:
            raise ValueError("outcome envelope fields were invalid")
        version = payload["schema_version"]
        if (
            isinstance(version, bool)
            or not isinstance(version, int)
            or version != EXTRACTION_OUTCOME_SCHEMA_VERSION
        ):
            raise ValueError("outcome schema version was unsupported")
        try:
            status = ExtractionOutcomeStatus(payload["status"])
        except (TypeError, ValueError) as exc:
            raise ValueError("outcome status was invalid") from exc

        result_payload = payload["result"]
        if result_payload is None:
            result = None
        elif isinstance(result_payload, Mapping):
            if set(result_payload) != _RESULT_FIELDS:
                raise ValueError("outcome result fields were invalid")
            summary_payload = result_payload["summary"]
            if (
                not isinstance(summary_payload, Mapping)
                or set(summary_payload) != _SUMMARY_FIELDS
            ):
                raise ValueError("outcome summary fields were invalid")
            result = ExtractionResult.from_dict(result_payload)
        else:
            raise TypeError("outcome result must be a mapping or None")

        diagnostic_payload = payload["diagnostic"]
        if diagnostic_payload is None:
            diagnostic = None
        elif isinstance(diagnostic_payload, Mapping):
            diagnostic = ExtractionDiagnostic.from_dict(diagnostic_payload)
        else:
            raise TypeError("outcome diagnostic must be a mapping or None")
        return cls(status=status, result=result, diagnostic=diagnostic)

    @classmethod
    def from_json(cls, payload: str) -> ExtractionOutcome:
        if not isinstance(payload, str):
            raise TypeError("outcome JSON must be a string")
        decoded = json.loads(payload)
        if not isinstance(decoded, Mapping):
            raise TypeError("outcome JSON must decode to an object")
        return cls.from_dict(decoded)


def _bounded_text(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("diagnostic text must be a string")
    normalized = " ".join(value.split())
    if not normalized:
        normalized = "Unknown extraction error."
    return normalized[:DIAGNOSTIC_TEXT_LIMIT]


def _validate_text(value: object, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{field} must be a non-empty string")
    if len(value) > DIAGNOSTIC_TEXT_LIMIT:
        raise ValueError(f"{field} exceeded the diagnostic text limit")
