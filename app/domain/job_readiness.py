from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from app.domain.output_path import OutputPathPlan


class ReadinessState(str, Enum):
    CHECKING = "checking"
    READY = "ready"
    WARNING = "warning"
    BLOCKED = "blocked"


class ReadinessSection(str, Enum):
    FILES = "files"
    OUTPUT = "output"
    AREAS = "areas"


class ReadinessIssueCode(str, Enum):
    NO_PDFS = "no_pdfs"
    PDF_SCAN_PENDING = "pdf_scan_pending"
    OUTPUT_PATH_MISSING = "output_path_missing"
    OUTPUT_EXTENSION_INVALID = "output_extension_invalid"
    OUTPUT_CHECK_PENDING = "output_check_pending"
    OUTPUT_CHECK_FAILED = "output_check_failed"
    OUTPUT_PARENT_MISSING = "output_parent_missing"
    OUTPUT_IS_DIRECTORY = "output_is_directory"
    OUTPUT_NOT_WRITABLE = "output_not_writable"
    OUTPUT_EXISTS = "output_exists"
    NO_NORMAL_AREAS = "no_normal_areas"
    INVALID_AREA_GEOMETRY = "invalid_area_geometry"
    DUPLICATE_AREA_TITLE = "duplicate_area_title"
    AMBIGUOUS_REFERENCE_FIELD = "ambiguous_reference_field"


@dataclass(frozen=True, slots=True)
class ReadinessIssue:
    code: ReadinessIssueCode
    section: ReadinessSection
    state: ReadinessState
    message: str
    action: str | None = None


@dataclass(frozen=True, slots=True)
class ReadinessSectionResult:
    section: ReadinessSection
    state: ReadinessState
    summary: str
    issues: tuple[ReadinessIssue, ...] = ()


@dataclass(frozen=True, slots=True)
class OutputReadinessProbe:
    """Filesystem-dependent output evidence for one exact target path."""

    path: Path
    path_key: str
    section: ReadinessSectionResult
    target_exists: bool = False
    plan: OutputPathPlan | None = None

    @property
    def planned_path(self) -> Path | None:
        return self.plan.planned_path if self.plan is not None else None


@dataclass(frozen=True, slots=True)
class JobReadinessResult:
    state: ReadinessState
    sections: tuple[ReadinessSectionResult, ...]
    output_plan: OutputPathPlan | None = None

    def section(self, section: ReadinessSection) -> ReadinessSectionResult:
        return next(item for item in self.sections if item.section is section)

    @property
    def issues(self) -> tuple[ReadinessIssue, ...]:
        return tuple(issue for section in self.sections for issue in section.issues)

    @property
    def first_blocker(self) -> ReadinessIssue | None:
        return next(
            (issue for issue in self.issues if issue.state is ReadinessState.BLOCKED),
            None,
        )

    @property
    def can_review(self) -> bool:
        return self.state in {ReadinessState.READY, ReadinessState.WARNING}


def aggregate_readiness_state(
    sections: tuple[ReadinessSectionResult, ...],
) -> ReadinessState:
    states = {item.state for item in sections}
    if ReadinessState.BLOCKED in states:
        return ReadinessState.BLOCKED
    if ReadinessState.CHECKING in states:
        return ReadinessState.CHECKING
    if ReadinessState.WARNING in states:
        return ReadinessState.WARNING
    return ReadinessState.READY
