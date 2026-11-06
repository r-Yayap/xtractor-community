from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets


class ElidedSelectableLabel(QtWidgets.QLabel):
    """A selectable label that retains the complete logical value."""

    def __init__(
        self,
        text: str = "",
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._full_text = ""
        self.setTextFormat(QtCore.Qt.PlainText)
        self.setTextInteractionFlags(
            QtCore.Qt.TextSelectableByMouse | QtCore.Qt.TextSelectableByKeyboard
        )
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Preferred,
        )
        self.setFullText(text)

    def setFullText(self, text: str) -> None:
        self._full_text = str(text)
        self.setToolTip(self._full_text)
        self.setAccessibleName(self._full_text)
        self._update_elision()

    def fullText(self) -> str:
        return self._full_text

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        self._update_elision()

    def _update_elision(self) -> None:
        available = max(1, self.contentsRect().width())
        display = self.fontMetrics().elidedText(
            self._full_text,
            QtCore.Qt.ElideMiddle,
            available,
        )
        super().setText(display)
