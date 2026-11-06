from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import json
from typing import Mapping


_MAX_RECENT_FILE_LENGTH = 255
_PHASE_ORDER = {
    "preparing": 0,
    "extracting": 1,
    "comparing": 2,
    "combining": 3,
    "writing_excel": 4,
    "finalizing": 5,
}


class ExtractionProgressPhase(str, Enum):
    PREPARING = "preparing"
    EXTRACTING = "extracting"
    COMBINING = "combining"
    WRITING_EXCEL = "writing_excel"
    FINALIZING = "finalizing"


def _required_nonnegative_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _optional_nonnegative_int(name: str, value: object) -> int | None:
    if value is None:
        return None
    return _required_nonnegative_int(name, value)


def _required_bool(name: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be a boolean")
    return value


def _optional_recent_file(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("recent_file must be a string or None")
    if not value:
        raise ValueError("recent_file must be a non-empty basename")
    if "/" in value or "\\" in value:
        raise ValueError("recent_file must be a basename without path separators")
    if len(value) > _MAX_RECENT_FILE_LENGTH:
        raise ValueError(
            f"recent_file must be {_MAX_RECENT_FILE_LENGTH} characters or fewer"
        )
    return value


@dataclass(frozen=True, slots=True)
class ExtractionProgressSnapshot:
    phase: ExtractionProgressPhase = ExtractionProgressPhase.PREPARING
    processed_files: int = 0
    total_files: int = 0
    succeeded_files: int = 0
    failed_files: int = 0
    processed_pages: int | None = None
    recent_file: str | None = None
    ocr_enabled: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.phase, ExtractionProgressPhase):
            raise TypeError("phase must be an ExtractionProgressPhase")
        processed_files = _required_nonnegative_int(
            "processed_files", self.processed_files
        )
        total_files = _required_nonnegative_int("total_files", self.total_files)
        succeeded_files = _required_nonnegative_int(
            "succeeded_files", self.succeeded_files
        )
        failed_files = _required_nonnegative_int("failed_files", self.failed_files)
        processed_pages = _optional_nonnegative_int(
            "processed_pages", self.processed_pages
        )
        recent_file = _optional_recent_file(self.recent_file)
        ocr_enabled = _required_bool("ocr_enabled", self.ocr_enabled)
        if processed_files > total_files:
            raise ValueError("processed_files must not exceed total_files")
        if succeeded_files + failed_files > processed_files:
            raise ValueError(
                "processed_files must be at least succeeded_files + failed_files"
            )
        object.__setattr__(self, "processed_files", processed_files)
        object.__setattr__(self, "total_files", total_files)
        object.__setattr__(self, "succeeded_files", succeeded_files)
        object.__setattr__(self, "failed_files", failed_files)
        object.__setattr__(self, "processed_pages", processed_pages)
        object.__setattr__(self, "recent_file", recent_file)
        object.__setattr__(self, "ocr_enabled", ocr_enabled)

    @property
    def percentage(self) -> int | None:
        if self.phase is not ExtractionProgressPhase.EXTRACTING or self.total_files <= 0:
            return None
        return int((self.processed_files * 100) / self.total_files)

    def with_phase(self, phase: ExtractionProgressPhase) -> ExtractionProgressSnapshot:
        return replace(self, phase=phase)

    def record_file(
        self,
        *,
        succeeded: bool,
        pages: int,
        recent_file: str,
    ) -> ExtractionProgressSnapshot:
        succeeded_value = _required_bool("succeeded", succeeded)
        pages_value = _required_nonnegative_int("pages", pages)
        recent_basename = _optional_recent_file(recent_file)
        current_pages = self.processed_pages or 0
        return replace(
            self,
            processed_files=self.processed_files + 1,
            succeeded_files=self.succeeded_files + int(succeeded_value),
            failed_files=self.failed_files + int(not succeeded_value),
            processed_pages=(
                current_pages + pages_value if succeeded_value else self.processed_pages
            ),
            recent_file=recent_basename,
        )

    def to_dict(self) -> dict[str, str | int | bool | None]:
        return {
            "phase": self.phase.value,
            "processed_files": self.processed_files,
            "total_files": self.total_files,
            "succeeded_files": self.succeeded_files,
            "failed_files": self.failed_files,
            "processed_pages": self.processed_pages,
            "recent_file": self.recent_file,
            "ocr_enabled": self.ocr_enabled,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> ExtractionProgressSnapshot:
        phase = payload.get("phase")
        if not isinstance(phase, str):
            raise TypeError("phase must be a string")
        try:
            progress_phase = ExtractionProgressPhase(phase)
        except ValueError as exc:
            raise ValueError(f"phase must be one of {[item.value for item in ExtractionProgressPhase]}") from exc
        return cls(
            phase=progress_phase,
            processed_files=payload.get("processed_files"),
            total_files=payload.get("total_files"),
            succeeded_files=payload.get("succeeded_files"),
            failed_files=payload.get("failed_files"),
            processed_pages=payload.get("processed_pages"),
            recent_file=payload.get("recent_file"),
            ocr_enabled=payload.get("ocr_enabled"),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_json(cls, payload: str) -> ExtractionProgressSnapshot:
        if not isinstance(payload, str):
            raise TypeError("json payload must be a string")
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError("json payload must be valid JSON") from exc
        if not isinstance(decoded, Mapping):
            raise TypeError("json payload must decode to a mapping")
        return cls.from_dict(decoded)


def is_progress_regression(
    current: ExtractionProgressSnapshot,
    candidate: ExtractionProgressSnapshot,
) -> bool:
    if not isinstance(current, ExtractionProgressSnapshot):
        raise TypeError("current must be an ExtractionProgressSnapshot")
    if not isinstance(candidate, ExtractionProgressSnapshot):
        raise TypeError("candidate must be an ExtractionProgressSnapshot")
    if _PHASE_ORDER[candidate.phase.value] < _PHASE_ORDER[current.phase.value]:
        return True
    if candidate.processed_files < current.processed_files:
        return True
    if candidate.total_files < current.total_files:
        return True
    if candidate.succeeded_files < current.succeeded_files:
        return True
    if candidate.failed_files < current.failed_files:
        return True
    if current.processed_pages is not None:
        if candidate.processed_pages is None:
            return True
        if candidate.processed_pages < current.processed_pages:
            return True
    return False
