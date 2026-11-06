from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from app.ui.theme import apply_shadow, get_theme


class DropFolderCard(QtWidgets.QFrame):
    def __init__(self, on_click, on_drop, parent=None):
        super().__init__(parent)
        self._on_click = on_click
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.setAcceptDrops(True)
        self._on_drop = on_drop
        self.setObjectName("dropFolderCard")
        self.setProperty("dragActive", False)
        self.setProperty("hovering", False)
        self.setFrameShape(QtWidgets.QFrame.NoFrame)
        apply_shadow(self, get_theme().tokens)

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.LeftButton:
            self._on_click()
        super().mousePressEvent(event)

    def _set_drag_active(self, active: bool) -> None:
        if self.property("dragActive") == active:
            return
        self.setProperty("dragActive", active)
        self.style().unpolish(self)
        self.style().polish(self)

    def dragEnterEvent(self, event: QtGui.QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            self._set_drag_active(True)
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragLeaveEvent(self, event: QtGui.QDragLeaveEvent) -> None:
        self._set_drag_active(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QtGui.QDropEvent) -> None:
        paths = [url.toLocalFile() for url in event.mimeData().urls()]
        self._on_drop(paths)
        self._set_drag_active(False)
        event.acceptProposedAction()

    def enterEvent(self, event: QtCore.QEvent) -> None:
        self.setProperty("hovering", True)
        self.style().unpolish(self)
        self.style().polish(self)
        super().enterEvent(event)

    def leaveEvent(self, event: QtCore.QEvent) -> None:
        self.setProperty("hovering", False)
        self.style().unpolish(self)
        self.style().polish(self)
        super().leaveEvent(event)


class FilesDropZone(QtWidgets.QFrame):
    def __init__(self, content: QtWidgets.QWidget, on_drop, parent=None):
        super().__init__(parent)
        self._on_drop = on_drop
        self._content = content
        self.setAcceptDrops(True)

        self._content.setAcceptDrops(False)
        if hasattr(self._content, "viewport"):
            self._content.viewport().setAcceptDrops(False)

        self._overlay = QtWidgets.QFrame(self)
        self._overlay.setObjectName("filesDropOverlay")
        self._overlay.setProperty("dragActive", False)
        self._overlay.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self._overlay.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self._overlay.hide()

        overlay_layout = QtWidgets.QVBoxLayout(self._overlay)
        overlay_layout.setContentsMargins(0, 0, 0, 0)
        overlay_layout.addStretch(1)
        overlay_label = QtWidgets.QLabel("Drop PDFs or Folder")
        overlay_label.setObjectName("filesDropOverlayLabel")
        overlay_label.setAlignment(QtCore.Qt.AlignCenter)
        overlay_layout.addWidget(overlay_label)
        overlay_layout.addStretch(1)

        stack = QtWidgets.QStackedLayout(self)
        stack.setStackingMode(QtWidgets.QStackedLayout.StackAll)
        stack.setContentsMargins(0, 0, 0, 0)
        stack.addWidget(self._content)
        stack.addWidget(self._overlay)

    def _set_drag_active(self, active: bool) -> None:
        if self._overlay.property("dragActive") == active and self._overlay.isVisible() == active:
            return
        self._overlay.setProperty("dragActive", active)
        self._overlay.setVisible(active)
        self._overlay.raise_()
        self._overlay.style().unpolish(self._overlay)
        self._overlay.style().polish(self._overlay)

    def dragEnterEvent(self, event: QtGui.QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            self._set_drag_active(True)
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event: QtGui.QDragMoveEvent) -> None:
        if event.mimeData().hasUrls():
            self._set_drag_active(True)
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dragLeaveEvent(self, event: QtGui.QDragLeaveEvent) -> None:
        self._set_drag_active(False)
        event.accept()

    def dropEvent(self, event: QtGui.QDropEvent) -> None:
        if event.mimeData().hasUrls():
            paths = [url.toLocalFile() for url in event.mimeData().urls()]
            self._on_drop(paths)
            self._set_drag_active(False)
            event.acceptProposedAction()
            return
        super().dropEvent(event)
