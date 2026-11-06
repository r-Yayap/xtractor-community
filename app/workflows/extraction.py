from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Sequence

from app.domain.models import (
    AreaSpec,
    ExtractionRequest,
    OcrSettings,
    normalize_area_spec,
)
from app.domain.output_path import OutputPathPlan
from app.domain.area_coordinates import CoordinateBasis


class ExtractionPreparationFailure(Enum):
    NO_AREAS_DEFINED = auto()
    INVALID_OUTPUT_PATH = auto()
    NO_PDFS_SELECTED = auto()
    INVALID_AREA_GEOMETRY = auto()
    DUPLICATE_AREA_TITLES = auto()


@dataclass(frozen=True, slots=True)
class ExtractionInputs:
    pdf_paths: Sequence[Path]
    output_excel: str | Path
    areas: Sequence[AreaSpec]
    ocr: OcrSettings
    coordinate_basis: CoordinateBasis | None = None
    area_validation_error: str | None = None

@dataclass(frozen=True, slots=True)
class PreparedExtraction:
    request: ExtractionRequest


@dataclass(frozen=True, slots=True)
class ExtractionPreparationResult:
    prepared: PreparedExtraction | None = None
    failure: ExtractionPreparationFailure | None = None


class ExtractionWorkflow:
    """Validate neutral UI state and build a spawn-safe extraction request."""

    def __init__(self, readiness_service) -> None:
        if readiness_service is None:
            raise TypeError("readiness_service is required")
        self.readiness_service = readiness_service

    def prepare(
        self,
        inputs: ExtractionInputs,
        *,
        output_probe_result=None,
        probe_output_if_missing: bool = True,
    ) -> ExtractionPreparationResult:
        from app.domain.job_readiness import ReadinessIssueCode
        readiness = self.readiness_service.evaluate(
            inputs,
            probe_output=(
                probe_output_if_missing and output_probe_result is None
            ),
            output_probe_result=output_probe_result,
        )
        blocker = readiness.first_blocker
        if blocker is not None:
            failure_by_code = {
                ReadinessIssueCode.NO_NORMAL_AREAS: ExtractionPreparationFailure.NO_AREAS_DEFINED,
                ReadinessIssueCode.NO_PDFS: ExtractionPreparationFailure.NO_PDFS_SELECTED,
                ReadinessIssueCode.INVALID_AREA_GEOMETRY: ExtractionPreparationFailure.INVALID_AREA_GEOMETRY,
                ReadinessIssueCode.DUPLICATE_AREA_TITLE: ExtractionPreparationFailure.DUPLICATE_AREA_TITLES,
            }
            return self._failure(
                failure_by_code.get(
                    blocker.code,
                    ExtractionPreparationFailure.INVALID_OUTPUT_PATH,
                )
            )

        request = self.build_request(
            inputs,
            output_plan=(
                getattr(output_probe_result, "plan", None)
                or getattr(readiness, "output_plan", None)
            ),
        )
        return ExtractionPreparationResult(prepared=PreparedExtraction(request))

    def build_request(
        self,
        inputs: ExtractionInputs,
        *,
        output_plan: OutputPathPlan | None = None,
    ) -> ExtractionRequest:
        """Build a request from already accepted inputs for compatibility callers."""
        output_path = Path(os.fspath(inputs.output_excel))
        areas = [
            normalize_area_spec(area, fallback_title=f"Area {index + 1}")
            for index, area in enumerate(inputs.areas)
        ]
        return ExtractionRequest(
            pdf_paths=list(inputs.pdf_paths),
            output_excel=output_path,
            areas=areas,
            ocr=inputs.ocr,
            coordinate_basis=inputs.coordinate_basis,
            output_plan=output_plan,
        )

    @staticmethod
    def _failure(
        failure: ExtractionPreparationFailure,
    ) -> ExtractionPreparationResult:
        return ExtractionPreparationResult(failure=failure)
