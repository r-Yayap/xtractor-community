from __future__ import annotations

import os
from pathlib import Path
import tempfile
from typing import Callable, Sequence

from app.domain.extraction_summary import (
    find_reference_column_indexes,
    normalized_output_title,
)
from app.domain.job_readiness import (
    JobReadinessResult,
    OutputReadinessProbe,
    ReadinessIssue,
    ReadinessIssueCode,
    ReadinessSection,
    ReadinessSectionResult,
    ReadinessState,
    aggregate_readiness_state,
)
from app.domain.models import (
    AreaSpec,
    AreaSpecValidationError,
    normalize_area_spec,
)
from app.domain.output_path import OutputPathPlan
from app.domain.area_coordinates import CoordinateBasis
from app.services.output_path_allocator import OutputPathAllocator


OutputProbe = Callable[[Path], str | None]


class JobReadinessService:
    """Evaluate cheap job configuration without opening selected PDFs."""

    def __init__(
        self,
        *,
        output_probe: OutputProbe | None = None,
        output_path_allocator: OutputPathAllocator | None = None,
    ) -> None:
        self._output_probe = output_probe or _probe_output_directory
        self._output_path_allocator = output_path_allocator or OutputPathAllocator()

    def evaluate(
        self,
        inputs,
        *,
        scan_pending: bool = False,
        probe_output: bool = False,
        output_probe_result: OutputReadinessProbe | None = None,
    ) -> JobReadinessResult:
        areas, area_errors = _normalize_areas(inputs.areas)
        if probe_output and output_probe_result is None:
            output_probe_result = self.probe_output(inputs.output_excel)
        sections = (
            self._files(inputs.pdf_paths, scan_pending),
            self._output(
                inputs.output_excel,
                output_probe_result,
            ),
            self._areas(
                areas,
                area_errors,
                basis=inputs.coordinate_basis,
                draft_error=inputs.area_validation_error,
            ),
        )
        return JobReadinessResult(
            aggregate_readiness_state(sections),
            sections,
            output_plan=(
                output_probe_result.plan
                if output_probe_result is not None
                else None
            ),
        )

    @staticmethod
    def _files(paths: Sequence[Path], scan_pending: bool) -> ReadinessSectionResult:
        if scan_pending:
            return _section(
                ReadinessSection.FILES,
                ReadinessState.CHECKING,
                "Scanning PDFs...",
                ReadinessIssueCode.PDF_SCAN_PENDING,
                "File discovery is still running.",
            )
        values = tuple(Path(path) for path in paths)
        if not values:
            return _section(
                ReadinessSection.FILES,
                ReadinessState.BLOCKED,
                "Select at least one PDF",
                ReadinessIssueCode.NO_PDFS,
                "Select at least one PDF before extracting.",
                "files",
            )
        return ReadinessSectionResult(
            ReadinessSection.FILES,
            ReadinessState.READY,
            f"{len(values)} PDF{'s' if len(values) != 1 else ''} selected",
        )

    def _output(
        self,
        value: str | Path,
        probe_result: OutputReadinessProbe | None,
    ) -> ReadinessSectionResult:
        text = os.fspath(value).strip()
        if not text:
            return _section(
                ReadinessSection.OUTPUT,
                ReadinessState.BLOCKED,
                "Choose an Excel output file",
                ReadinessIssueCode.OUTPUT_PATH_MISSING,
                "Choose where the Excel result should be saved.",
                "output",
            )
        path = Path(text)
        if path.suffix.casefold() != ".xlsx":
            return _section(
                ReadinessSection.OUTPUT,
                ReadinessState.BLOCKED,
                "Output must be an .xlsx file",
                ReadinessIssueCode.OUTPUT_EXTENSION_INVALID,
                "Choose an output filename ending in .xlsx.",
                "output",
            )
        path_key = _path_key(path)
        if probe_result is None or probe_result.path_key != path_key:
            return _section(
                ReadinessSection.OUTPUT,
                ReadinessState.CHECKING,
                "Checking output folder...",
                ReadinessIssueCode.OUTPUT_CHECK_PENDING,
                "Wait for the output folder check to finish.",
            )
        return probe_result.section

    def probe_output(self, value: str | Path) -> OutputReadinessProbe:
        """Perform all output filesystem access for an explicit caller."""
        text = os.fspath(value).strip()
        path = Path(text)
        path_key = _path_key(path)
        if not text or path.suffix.casefold() != ".xlsx":
            # The synchronous evaluator owns syntax messages. This branch keeps
            # the probe total for worker callers racing with an edit.
            section = _section(
                ReadinessSection.OUTPUT,
                ReadinessState.BLOCKED,
                "Output path changed",
                ReadinessIssueCode.OUTPUT_EXTENSION_INVALID,
                "Choose an output filename ending in .xlsx.",
                "output",
            )
            return OutputReadinessProbe(path, path_key, section)

        target_exists = path.exists()
        if target_exists and path.is_dir():
            section = _section(
                ReadinessSection.OUTPUT,
                ReadinessState.BLOCKED,
                "Output points to a folder",
                ReadinessIssueCode.OUTPUT_IS_DIRECTORY,
                "Choose an Excel filename, not a folder.",
                "output",
            )
            return OutputReadinessProbe(path, path_key, section, True)
        parent = path.parent
        if not parent.is_dir():
            section = _section(
                ReadinessSection.OUTPUT,
                ReadinessState.BLOCKED,
                "Output folder is unavailable",
                ReadinessIssueCode.OUTPUT_PARENT_MISSING,
                "Choose an existing output folder.",
                "output",
            )
            return OutputReadinessProbe(path, path_key, section, target_exists)
        message = self._output_probe(parent)
        if message:
            section = _section(
                ReadinessSection.OUTPUT,
                ReadinessState.BLOCKED,
                "Output folder is not writable",
                ReadinessIssueCode.OUTPUT_NOT_WRITABLE,
                message,
                "output",
            )
            return OutputReadinessProbe(path, path_key, section, target_exists)
        if target_exists:
            plan = self._output_path_allocator.plan(path)
            planned_path = plan.planned_path
            issue = ReadinessIssue(
                ReadinessIssueCode.OUTPUT_EXISTS,
                ReadinessSection.OUTPUT,
                ReadinessState.WARNING,
                "The existing file will be preserved. "
                f"Xtractor will create a new output at {planned_path}.",
            )
            section = ReadinessSectionResult(
                section=ReadinessSection.OUTPUT,
                state=ReadinessState.READY,
                summary=(
                    f"{path} already exists. Existing file will be preserved. "
                    f"New output: {planned_path}"
                ),
                issues=(issue,),
            )
        else:
            plan = OutputPathPlan(path, path, False)
            section = ReadinessSectionResult(
                ReadinessSection.OUTPUT,
                ReadinessState.READY,
                str(path),
            )
        return OutputReadinessProbe(path, path_key, section, target_exists, plan)

    @staticmethod
    def output_probe_failure(
        value: str | Path,
        message: str,
    ) -> OutputReadinessProbe:
        path = Path(os.fspath(value).strip())
        section = _section(
            ReadinessSection.OUTPUT,
            ReadinessState.BLOCKED,
            "Output folder check failed",
            ReadinessIssueCode.OUTPUT_CHECK_FAILED,
            message or "The output folder could not be checked.",
            "output",
        )
        return OutputReadinessProbe(path, _path_key(path), section)

    @staticmethod
    def _areas(
        areas: tuple[AreaSpec, ...],
        errors: tuple[str, ...],
        *,
        basis: CoordinateBasis | None = None,
        draft_error: str | None = None,
    ) -> ReadinessSectionResult:
        issues: list[ReadinessIssue] = []
        all_errors = tuple(
            dict.fromkeys(
                message
                for message in (*errors, draft_error)
                if message
            )
        )
        if not areas and not all_errors:
            issues.append(ReadinessIssue(
                ReadinessIssueCode.NO_NORMAL_AREAS,
                ReadinessSection.AREAS,
                ReadinessState.BLOCKED,
                "At least one extraction area is required.",
                "areas",
            ))
        issues.extend(
            ReadinessIssue(
                ReadinessIssueCode.INVALID_AREA_GEOMETRY,
                ReadinessSection.AREAS,
                ReadinessState.BLOCKED,
                message,
                "areas",
            )
            for message in all_errors
        )
        if basis is not None:
            for area in areas:
                x0, y0, x1, y1 = area.rect
                if min(x0, y0) < 0 or x1 > basis.width or y1 > basis.height:
                    issues.append(ReadinessIssue(
                        ReadinessIssueCode.INVALID_AREA_GEOMETRY,
                        ReadinessSection.AREAS,
                        ReadinessState.BLOCKED,
                        f"{area.title} extends beyond the reference page.",
                        "areas",
                    ))
        normalized: dict[str, list[str]] = {}
        for area in areas:
            normalized.setdefault(normalized_output_title(area.title), []).append(area.title)
        collisions = tuple(values for key, values in normalized.items() if key and len(values) > 1)
        if collisions:
            labels = ", ".join(" / ".join(values) for values in collisions)
            issues.append(ReadinessIssue(
                ReadinessIssueCode.DUPLICATE_AREA_TITLE,
                ReadinessSection.AREAS,
                ReadinessState.BLOCKED,
                f"Duplicate output field titles: {labels}.",
                "areas",
            ))
        reference_indexes = find_reference_column_indexes([area.title for area in areas])
        if len(reference_indexes) > 1:
            names = ", ".join(areas[index].title for index in reference_indexes)
            issues.append(ReadinessIssue(
                ReadinessIssueCode.AMBIGUOUS_REFERENCE_FIELD,
                ReadinessSection.AREAS,
                ReadinessState.WARNING,
                f"Multiple fields look like the primary reference: {names}. Xtractor will use {areas[reference_indexes[0]].title}.",
                "areas",
            ))
        state = _issues_state(issues)
        return ReadinessSectionResult(
            ReadinessSection.AREAS,
            state,
            f"{len(areas)} extraction field{'s' if len(areas) != 1 else ''} configured",
            tuple(issues),
        )

def _section(
    section: ReadinessSection,
    state: ReadinessState,
    summary: str,
    code: ReadinessIssueCode,
    message: str,
    action: str | None = None,
) -> ReadinessSectionResult:
    return ReadinessSectionResult(
        section,
        state,
        summary,
        (ReadinessIssue(code, section, state, message, action),),
    )


def _issues_state(issues: Sequence[ReadinessIssue]) -> ReadinessState:
    if any(issue.state is ReadinessState.BLOCKED for issue in issues):
        return ReadinessState.BLOCKED
    if any(issue.state is ReadinessState.WARNING for issue in issues):
        return ReadinessState.WARNING
    return ReadinessState.READY


def _normalize_areas(values) -> tuple[tuple[AreaSpec, ...], tuple[str, ...]]:
    areas: list[AreaSpec] = []
    errors: list[str] = []
    for index, value in enumerate(values):
        area, error = _normalize_area(value, f"Area {index + 1}")
        if area is not None:
            areas.append(area)
        if error:
            errors.append(error)
    return tuple(areas), tuple(errors)


def _normalize_area(
    value: object,
    fallback_title: str,
) -> tuple[AreaSpec | None, str | None]:
    try:
        return normalize_area_spec(value, fallback_title=fallback_title), None
    except AreaSpecValidationError as exc:
        return None, str(exc)


def _probe_output_directory(parent: Path) -> str | None:
    probe_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".xtractor-readiness-",
            suffix=".tmp",
            dir=parent,
            delete=False,
        ) as probe:
            probe_path = Path(probe.name)
            probe.write(b"x")
    except OSError:
        return "The output folder is not writable."
    finally:
        if probe_path is not None:
            try:
                probe_path.unlink(missing_ok=True)
            except OSError:
                pass
    return None


def _path_key(path: Path) -> str:
    """Normalize path text without resolving or touching the filesystem."""
    return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path))))
