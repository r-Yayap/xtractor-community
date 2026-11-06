from __future__ import annotations

import os
import threading
from collections.abc import Sequence

from PySide6 import QtCore

from app.services.pdf_selection_service import (
    PdfScanBranch,
    build_pdf_scan_manifest,
    scan_pdf_sources,
)


class PdfManifestSignals(QtCore.QObject):
    result = QtCore.Signal(int, object)
    failed = QtCore.Signal(int, str)


class PdfManifestWorker(QtCore.QRunnable):
    def __init__(
        self,
        request_id: int,
        sources: Sequence[str | os.PathLike[str]],
        synthetic_root_name: str | None,
    ) -> None:
        super().__init__()
        self.setAutoDelete(False)
        self.request_id = request_id
        self.sources = list(sources)
        self.synthetic_root_name = synthetic_root_name
        self.signals = PdfManifestSignals()

    @QtCore.Slot()
    def run(self) -> None:
        try:
            result = build_pdf_scan_manifest(
                self.sources,
                synthetic_root_name=self.synthetic_root_name,
            )
        except Exception as exc:
            self.signals.failed.emit(self.request_id, str(exc))
            return
        self.signals.result.emit(self.request_id, result)


class PdfBranchSignals(QtCore.QObject):
    progress = QtCore.Signal(int, str, int)
    result = QtCore.Signal(int, str, object)
    failed = QtCore.Signal(int, str, str)


class PdfBranchWorker(QtCore.QRunnable):
    def __init__(
        self,
        request_id: int,
        branch: PdfScanBranch,
        cancel_event: threading.Event,
    ) -> None:
        super().__init__()
        self.setAutoDelete(False)
        self.request_id = request_id
        self.branch = branch
        self.cancel_event = cancel_event
        self.signals = PdfBranchSignals()

    @QtCore.Slot()
    def run(self) -> None:
        try:
            result = scan_pdf_sources(
                [self.branch.path],
                cancel_event=self.cancel_event,
                progress=lambda count: self.signals.progress.emit(
                    self.request_id,
                    self.branch.branch_id,
                    count,
                ),
            )
        except Exception as exc:
            self.signals.failed.emit(
                self.request_id,
                self.branch.branch_id,
                str(exc),
            )
            return
        self.signals.result.emit(
            self.request_id,
            self.branch.branch_id,
            result,
        )


class PdfScanSignals(QtCore.QObject):
    progress = QtCore.Signal(int, int)
    result = QtCore.Signal(int, object)
    failed = QtCore.Signal(int, str)


class PdfScanWorker(QtCore.QRunnable):
    def __init__(
        self,
        request_id: int,
        sources: Sequence[str | os.PathLike[str]],
        synthetic_root_name: str | None,
        cancel_event: threading.Event,
    ) -> None:
        super().__init__()
        self.setAutoDelete(False)
        self.request_id = request_id
        self.sources = list(sources)
        self.synthetic_root_name = synthetic_root_name
        self.cancel_event = cancel_event
        self.signals = PdfScanSignals()

    @QtCore.Slot()
    def run(self) -> None:
        try:
            result = scan_pdf_sources(
                self.sources,
                synthetic_root_name=self.synthetic_root_name,
                cancel_event=self.cancel_event,
                progress=lambda count: self.signals.progress.emit(
                    self.request_id,
                    count,
                ),
            )
        except Exception as exc:
            self.signals.failed.emit(self.request_id, str(exc))
            return
        self.signals.result.emit(self.request_id, result)
