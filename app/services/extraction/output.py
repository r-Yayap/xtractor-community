from __future__ import annotations

import csv
import logging
import os
from pathlib import Path
from typing import Mapping, Sequence

from app.domain.file_manifest import FileManifestEntry
from app.domain.output_path import OutputPathPlan
from app.domain.extraction_schema import staged_header
from app.infra.excel_writer import write_from_csv
from app.infra.suppression_audit import SUPPRESSED_TEXT_HEADERS
from app.services.output_path_allocator import OutputPathAllocator


logger = logging.getLogger(__name__)

_MAX_PUBLICATION_ATTEMPTS = 100_000


def _unlink_consumed(path: Path, artifact: str) -> None:
    try:
        path.unlink()
    except OSError as exc:
        logger.debug(
            "Could not remove consumed %s (%s)",
            artifact,
            type(exc).__name__,
        )


class ExtractionOutputWriter:
    """Combine worker artifacts and hand one stable schema to Excel output."""

    @staticmethod
    def combine_worker_rows(
        temp_dir: Path,
        combined_csv: Path,
        unique_headers: Mapping[int, str],
    ) -> None:
        area_headers = [unique_headers[index] for index in range(len(unique_headers))]
        worker_paths = sorted(Path(temp_dir).glob("temp_*.csv"))
        with combined_csv.open("w", newline="", encoding="utf-8") as output:
            writer = csv.writer(output)
            writer.writerow(staged_header(area_headers))
            for worker_path in worker_paths:
                with worker_path.open(newline="", encoding="utf-8") as source:
                    writer.writerows(csv.reader(source))
                _unlink_consumed(worker_path, "worker staging")

    @staticmethod
    def combine_suppression_audits(
        temp_dir: Path,
        combined_csv: Path,
    ) -> int:
        return ExtractionOutputWriter._combine_audits(
            Path(temp_dir).glob("suppressed_*.csv"),
            combined_csv,
            SUPPRESSED_TEXT_HEADERS,
        )

    @staticmethod
    def _combine_audits(paths, combined_csv: Path, headers: Sequence[str]) -> int:
        combined_csv = Path(combined_csv)
        worker_csvs = sorted(path for path in paths if path != combined_csv)
        count = 0
        with combined_csv.open("w", newline="", encoding="utf-8") as output_stream:
            writer = csv.writer(output_stream)
            writer.writerow(headers)
            for worker_csv in worker_csvs:
                with worker_csv.open(newline="", encoding="utf-8") as input_stream:
                    for row in csv.reader(input_stream):
                        writer.writerow(row)
                        count += 1
                _unlink_consumed(worker_csv, "audit staging")
        return count

    @staticmethod
    def write(
        *,
        combined_csv: Path,
        output_excel: Path,
        temp_dir: Path,
        unique_headers: Mapping[int, str],
        needs_images: bool,
        pdf_root: Path,
        metrics: Sequence[tuple[str, object]],
        suppressed_csv: Path,
        file_manifest: Sequence[FileManifestEntry] = (),
        base_order: Sequence[object] | None = None,
        output_plan: OutputPathPlan | None = None,
        allocator: OutputPathAllocator | None = None,
    ) -> Path:
        requested = Path(output_excel)
        allocator = allocator or OutputPathAllocator()
        preferred = None
        if output_plan is not None and _same_path(
            requested,
            output_plan.requested_path,
        ):
            preferred = output_plan.planned_path

        for _attempt in range(_MAX_PUBLICATION_ATTEMPTS):
            plan = allocator.plan(requested, preferred_path=preferred)
            try:
                output = write_from_csv(
                    combined_csv,
                    plan.planned_path,
                    temp_dir,
                    dict(unique_headers),
                    needs_images,
                    pdf_root,
                    list(metrics),
                    suppressed_csv=suppressed_csv,
                    file_manifest=file_manifest,
                    base_order=base_order,
                )
            except FileExistsError:
                if not _path_occupied(plan.planned_path):
                    raise
                preferred = plan.planned_path
                continue
            output = Path(output)
            if not _same_path(output, plan.planned_path):
                raise RuntimeError(
                    "Excel writer returned a path different from the allocated output."
                )
            return output
        raise RuntimeError("Unable to publish a unique extraction output path.")


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(
        os.path.normpath(os.path.abspath(os.fspath(left)))
    ) == os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(right))))


def _path_occupied(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    return True
