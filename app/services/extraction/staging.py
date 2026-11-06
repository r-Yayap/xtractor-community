from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Iterable


logger = logging.getLogger(__name__)


class WorkerStagingWriter:
    """Own one worker's row and unpublished audit streams."""

    def __init__(self, temp_dir: Path, prefix: str) -> None:
        self.temp_dir = Path(temp_dir)
        self.prefix = str(prefix)
        self.primary_path = self.temp_dir / f"temp_{self.prefix}.csv"
        self.suppression_staging_path = (
            self.temp_dir / f".suppressed_{self.prefix}.csv.tmp"
        )
        self.suppression_published_path = (
            self.temp_dir / f"suppressed_{self.prefix}.csv"
        )
        self._primary_file = open(
            self.primary_path, "w", newline="", encoding="utf-8"
        )
        self._primary_writer = csv.writer(self._primary_file)
        self._suppression_file = None
        self._suppression_writer = None

    def write_row(self, row: Iterable[object]) -> None:
        self._primary_writer.writerow(row)

    def write_suppression(self, row: Iterable[object]) -> None:
        if self._suppression_writer is None:
            self._suppression_file = open(
                self.suppression_staging_path,
                "w",
                newline="",
                encoding="utf-8",
                buffering=1,
            )
            self._suppression_writer = csv.writer(self._suppression_file)
        self._suppression_writer.writerow(row)

    def flush_primary(self) -> None:
        self._primary_file.flush()

    def close(self, *, primary_exception_active: bool) -> None:
        close_errors: list[tuple[str, Exception]] = []
        for name, stream in (
            ("suppression", self._suppression_file),
        ):
            if stream is None:
                continue
            try:
                stream.flush()
            except Exception as exc:
                close_errors.append((f"{name} stream flush", exc))
            try:
                stream.close()
            except Exception as exc:
                close_errors.append((f"{name} stream close", exc))
        try:
            self._primary_file.close()
        except Exception as exc:
            close_errors.append(("primary stream close", exc))
        if not close_errors:
            return
        if primary_exception_active:
            for operation, exception in close_errors:
                logger.warning(
                    "Worker staging %s failed during close (%s)",
                    operation,
                    type(exception).__name__,
                )
            return
        primary_close_error = next(
            (
                exception
                for operation, exception in close_errors
                if operation == "primary stream close"
            ),
            None,
        )
        raise primary_close_error or close_errors[0][1]

    def publish_audits(self) -> None:
        if self.suppression_staging_path.is_file():
            self.suppression_staging_path.replace(self.suppression_published_path)

    def discard_audits(self) -> None:
        for path in (self.suppression_staging_path,):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                logger.debug(
                    "Could not discard worker audit staging (%s)",
                    type(exc).__name__,
                )
