from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6 import QtCore

from app.services.job_readiness_service import JobReadinessService


class OutputReadinessSignals(QtCore.QObject):
    result = QtCore.Signal(int, object)
    failed = QtCore.Signal(int, str)


class OutputReadinessWorker(QtCore.QRunnable):
    """Run network-capable output checks away from the Qt GUI thread."""

    def __init__(
        self,
        *,
        request_id: int,
        output_path: Path,
        service_factory: Callable[[], JobReadinessService] = JobReadinessService,
    ) -> None:
        super().__init__()
        self.request_id = request_id
        self.output_path = Path(output_path)
        self.service_factory = service_factory
        self.signals = OutputReadinessSignals()

    @QtCore.Slot()
    def run(self) -> None:
        try:
            result = self.service_factory().probe_output(self.output_path)
        except Exception as exc:
            self.signals.failed.emit(
                self.request_id,
                str(exc) or type(exc).__name__,
            )
            return
        self.signals.result.emit(self.request_id, result)
