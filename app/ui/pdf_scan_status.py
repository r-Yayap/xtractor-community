from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets


class BusySpinner(QtWidgets.QWidget):
    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(20, 20)
        self._frame = 0
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(75)
        self._timer.timeout.connect(self._advance)
        self.hide()

    @property
    def is_spinning(self) -> bool:
        return self._timer.isActive()

    def start(self) -> None:
        self.show()
        if not self._timer.isActive():
            self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        self._frame = 0
        self.hide()
        self.update()

    def _advance(self) -> None:
        self._frame = (self._frame + 1) % 12
        self.update()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        del event
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.translate(self.width() / 2, self.height() / 2)
        color = self.palette().color(QtGui.QPalette.Highlight)
        for index in range(12):
            distance = (index - self._frame) % 12
            stroke = QtGui.QColor(color)
            stroke.setAlpha(max(35, 255 - distance * 18))
            pen = QtGui.QPen(stroke, 2, QtCore.Qt.SolidLine, QtCore.Qt.RoundCap)
            painter.setPen(pen)
            painter.drawLine(QtCore.QPointF(0, -4), QtCore.QPointF(0, -8))
            painter.rotate(30)


class PdfScanStatusPanel(QtWidgets.QFrame):
    cancel_requested = QtCore.Signal()

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("pdfScanStatusPanel")
        self.setProperty("scanState", "scanning")

        self.spinner = BusySpinner(self)
        self.headline = QtWidgets.QLabel()
        self.headline.setObjectName("pdfScanStatusHeadline")
        self.summary = QtWidgets.QLabel()
        self.summary.setObjectName("pdfScanStatusSummary")
        self.detail = QtWidgets.QLabel()
        self.detail.setObjectName("pdfScanStatusDetail")
        self.detail.setWordWrap(True)
        self.cancel_button = QtWidgets.QPushButton("Cancel scan")
        self.cancel_button.setObjectName("pdfScanCancelButton")
        self.cancel_button.clicked.connect(self.cancel_requested)

        header = QtWidgets.QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(self.spinner)
        header.addWidget(self.headline, 1)
        header.addWidget(self.cancel_button)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(header)
        layout.addWidget(self.summary)
        layout.addWidget(self.detail)

        self._hide_timer = QtCore.QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(2500)
        self._hide_timer.timeout.connect(self.hide)
        self.hide()

    @staticmethod
    def _pdf_count(found_count: int) -> str:
        return f"{found_count} PDF" if found_count == 1 else f"{found_count} PDFs"

    @staticmethod
    def _folder_count(pending_count: int) -> str:
        return "1 folder" if pending_count == 1 else f"{pending_count} folders"

    def _set_state(self, state: str) -> None:
        self.setProperty("scanState", state)
        self.style().unpolish(self)
        self.style().polish(self)

    def set_scanning(
        self,
        found_count: int,
        pending_count: int,
        stalled_names: list[str] | tuple[str, ...],
    ) -> None:
        self._hide_timer.stop()
        self._set_state("scanning")
        self.headline.setText("Scanning PDF folders…")
        self.summary.setText(
            f"Found {self._pdf_count(found_count)} • "
            f"{self._folder_count(pending_count)} still scanning"
        )
        visible_names = list(stalled_names[:2])
        detail = f"{', '.join(visible_names)} — Still scanning" if visible_names else ""
        remaining = len(stalled_names) - len(visible_names)
        if remaining:
            noun = "folder" if remaining == 1 else "folders"
            detail += f" • +{remaining} more {noun}"
        self.detail.setText(detail)
        self.detail.setVisible(bool(detail))
        self.cancel_button.show()
        self.show()
        self.spinner.start()

    def set_complete(self, found_count: int) -> None:
        self._set_state("complete")
        self.spinner.stop()
        self.headline.setText("PDF scan complete")
        self.summary.setText(f"Found {self._pdf_count(found_count)}")
        self.detail.clear()
        self.detail.hide()
        self.cancel_button.hide()
        self.show()
        self._hide_timer.start()

    def set_cancelled(self, found_count: int) -> None:
        self._hide_timer.stop()
        self._set_state("cancelled")
        self.spinner.stop()
        self.headline.setText("PDF scan cancelled")
        self.summary.setText(
            f"{self._pdf_count(found_count)} available from completed folders"
        )
        self.detail.setText("Results may be incomplete.")
        self.detail.show()
        self.cancel_button.hide()
        self.show()

    def set_incomplete(self, found_count: int, failed_count: int) -> None:
        self._hide_timer.stop()
        self._set_state("incomplete")
        self.spinner.stop()
        self.headline.setText("PDF scan incomplete")
        failed_folders = (
            "1 folder failed" if failed_count == 1 else f"{failed_count} folders failed"
        )
        self.summary.setText(
            f"{self._pdf_count(found_count)} available • {failed_folders}"
        )
        self.detail.setText("Review the red folders below or try scanning them again.")
        self.detail.show()
        self.cancel_button.hide()
        self.show()

    def set_failed(self, message: str) -> None:
        self._hide_timer.stop()
        self._set_state("failed")
        self.spinner.stop()
        self.headline.setText("PDF scan failed")
        self.summary.setText("The selected PDF sources could not be scanned.")
        self.detail.setText(message)
        self.detail.setVisible(bool(message))
        self.cancel_button.hide()
        self.show()
