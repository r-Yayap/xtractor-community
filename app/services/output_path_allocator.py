from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Callable

from app.domain.output_path import OutputPathPlan


_TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"
_TIMESTAMP_PATTERN = re.compile(
    r"^(?P<stem>.+)_(?P<timestamp>\d{8}_\d{6})(?:_(?P<suffix>\d+))?$",
    re.IGNORECASE,
)
_MAX_CANDIDATES = 100_000


class OutputPathAllocationError(OSError):
    """Raised when no safe candidate can be found in one allocation series."""


class OutputPathAllocator:
    """Own the extraction workbook naming policy without rendering workbooks."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or datetime.now

    def plan(
        self,
        requested_path: str | os.PathLike[str],
        *,
        preferred_path: str | os.PathLike[str] | None = None,
    ) -> OutputPathPlan:
        requested = Path(requested_path)
        preferred = Path(preferred_path) if preferred_path is not None else None

        if preferred is not None and self._is_valid_preferred(requested, preferred):
            if not _path_exists(preferred):
                return OutputPathPlan(requested, preferred, requested != preferred)
            series = _timestamp_series(preferred, requested)
            if series is not None:
                timestamp, suffix = series
                return self._next_candidate(requested, timestamp, suffix + 1)

        if preferred is None and not _path_exists(requested):
            return OutputPathPlan(requested, requested, False)
        if preferred is not None and preferred == requested and not _path_exists(requested):
            return OutputPathPlan(requested, requested, False)

        timestamp = self._clock().strftime(_TIMESTAMP_FORMAT)
        return self._next_candidate(requested, timestamp, 1)

    def revalidate(self, plan: OutputPathPlan) -> OutputPathPlan:
        """Keep a free displayed plan, or allocate its next safe sibling."""

        return self.plan(plan.requested_path, preferred_path=plan.planned_path)

    @staticmethod
    def _is_valid_preferred(requested: Path, preferred: Path) -> bool:
        return (
            preferred.parent == requested.parent
            and preferred.suffix.casefold() == requested.suffix.casefold()
            and (
                preferred == requested
                or _timestamp_series(preferred, requested) is not None
            )
        )

    def _next_candidate(
        self,
        requested: Path,
        timestamp: str,
        start_suffix: int,
    ) -> OutputPathPlan:
        for suffix in range(max(1, int(start_suffix)), _MAX_CANDIDATES + 1):
            suffix_text = "" if suffix == 1 else f"_{suffix}"
            candidate = requested.with_name(
                f"{requested.stem}_{timestamp}{suffix_text}{requested.suffix}"
            )
            if not _path_exists(candidate):
                return OutputPathPlan(requested, candidate, True)
        raise OutputPathAllocationError(
            "No unique extraction output path is available for the requested filename."
        )


def _timestamp_series(candidate: Path, requested: Path) -> tuple[str, int] | None:
    if candidate.parent != requested.parent:
        return None
    if candidate.suffix.casefold() != requested.suffix.casefold():
        return None
    match = _TIMESTAMP_PATTERN.fullmatch(candidate.stem)
    if match is None or match.group("stem").casefold() != requested.stem.casefold():
        return None
    suffix = match.group("suffix")
    return match.group("timestamp"), int(suffix) if suffix is not None else 1


def _path_exists(path: Path) -> bool:
    """Use lstat so dangling links and other occupied entries stay protected."""

    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    return True
