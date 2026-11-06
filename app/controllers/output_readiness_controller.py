from __future__ import annotations

from dataclasses import dataclass, replace
import os
from pathlib import Path

from app.domain.job_readiness import OutputReadinessProbe
from app.services.job_readiness_service import JobReadinessService


@dataclass(frozen=True, slots=True)
class OutputProbeRequest:
    request_id: int
    path: Path


@dataclass(frozen=True, slots=True)
class OutputReadinessState:
    request_id: int = 0
    path: Path | None = None
    probe_result: OutputReadinessProbe | None = None
    closing: bool = False


class OutputReadinessController:
    """Own output-probe identity and evidence independently of Qt scheduling."""

    def __init__(self) -> None:
        self._state = OutputReadinessState()

    @property
    def state(self) -> OutputReadinessState:
        return self._state

    def change_path(self, value: str | os.PathLike[str]) -> OutputProbeRequest | None:
        if self._state.closing:
            return None
        text = os.fspath(value).strip()
        path = Path(text) if text else None
        request_id = self._state.request_id + 1
        self._state = OutputReadinessState(
            request_id=request_id,
            path=path,
        )
        if path is None or path.suffix.casefold() != ".xlsx":
            return None
        return OutputProbeRequest(request_id=request_id, path=path)

    def accept_result(
        self,
        request_id: int,
        result: OutputReadinessProbe,
    ) -> bool:
        if not self._is_current(request_id):
            return False
        self._state = replace(self._state, probe_result=result)
        return True

    def accept_failure(self, request_id: int, message: str) -> bool:
        if not self._is_current(request_id) or self._state.path is None:
            return False
        result = JobReadinessService.output_probe_failure(
            self._state.path,
            message,
        )
        self._state = replace(self._state, probe_result=result)
        return True

    def close(self) -> None:
        self._state = OutputReadinessState(
            request_id=self._state.request_id + 1,
            path=self._state.path,
            closing=True,
        )

    def _is_current(self, request_id: int) -> bool:
        return not self._state.closing and request_id == self._state.request_id
