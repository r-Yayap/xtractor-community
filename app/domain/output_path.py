from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class OutputPathPlan:
    """Immutable distinction between the user's path and the planned output."""

    requested_path: Path
    planned_path: Path
    collision: bool = False

    @property
    def uses_alternate_path(self) -> bool:
        return self.requested_path != self.planned_path
