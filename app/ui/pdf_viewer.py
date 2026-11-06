# pdf_viewer.py
from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Optional

from PySide6 import QtCore, QtGui, QtWidgets, QtSvg

from app.infra.pdf import create_pdf_backend
from app.infra.pdf.contracts import PdfBackend, PdfDocumentSession
from app.infra.pdf.errors import EmptyDocumentError
from app.domain.models import (
    AreaSpec,
    AreaSpecValidationError,
    normalize_area_spec,
)
from app.ui.constants import CURRENT_ZOOM, SCROLL_MARGIN, SCROLL_INCREMENT_THRESHOLD
from app.ui.theme import get_theme
from app.domain.area_coordinates import (
    CoordinateBasis,
    CoordinateTransform,
)
from app.ui.viewer_editing import (
    AreaSnapshot,
    Coordinates,
    ViewerEdit,
    ViewerEditHistory,
    ViewerStateSnapshot,
)


LOGGER = logging.getLogger(__name__)


class PageNumberSpinBox(QtWidgets.QSpinBox):
    """Keep page-entry text intact until the viewer validates the commit."""

    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()

    def validate(
        self,
        text: str,
        position: int,
    ) -> tuple[QtGui.QValidator.State, str, int]:
        return QtGui.QValidator.State.Acceptable, text, position

    def valueFromText(self, text: str) -> int:
        raw_value = text.strip()
        if raw_value.isascii() and raw_value.isdecimal():
            page_number = int(raw_value)
            if self.minimum() <= page_number <= self.maximum():
                return page_number
        return self.value()


class PillBar(QtWidgets.QFrame):
    """A pill-shaped container that *clips its children* using a mask."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("viewerZoomBar")
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.setAttribute(QtCore.Qt.WA_NoSystemBackground, True)
        self._radius = 0

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        r = max(1, self.height() // 2)
        self._radius = r

        # Build a rounded-rect mask (clips children too)
        path = QtGui.QPainterPath()
        path.addRoundedRect(QtCore.QRectF(self.rect()), r, r)
        poly = path.toFillPolygon().toPolygon()
        self.setMask(QtGui.QRegion(poly))

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        theme = get_theme()
        colors = theme.tokens.get("colors", {})

        bg = QtGui.QColor(colors.get("surface_alt", "#2b2b2b"))
        border = QtGui.QColor(colors.get("border", "#444444"))

        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)

        rect = self.rect().adjusted(0, 0, -1, -1)
        p.setPen(QtGui.QPen(border, 1))
        p.setBrush(QtGui.QBrush(bg))
        p.drawRoundedRect(rect, self._radius, self._radius)


class MagnifierIcon(QtWidgets.QWidget):
    def __init__(self, parent=None, color: QtGui.QColor | None = None):
        super().__init__(parent)
        self.setFixedSize(18, 18)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self._color = color or QtGui.QColor("#ffffff")

    def setColor(self, color: QtGui.QColor) -> None:
        self._color = color
        self.update()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)

        pen = QtGui.QPen(self._color, 2)
        pen.setCapStyle(QtCore.Qt.RoundCap)
        pen.setJoinStyle(QtCore.Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(QtCore.Qt.NoBrush)

        w, h = self.width(), self.height()

        # Lens
        lens_rect = QtCore.QRectF(3, 3, w - 9, h - 9)
        p.drawEllipse(lens_rect)

        # Handle
        p.drawLine(QtCore.QPointF(w - 7, h - 7), QtCore.QPointF(w - 3, h - 3))


class HoverRevealToolButton(QtWidgets.QPushButton):
    COLLAPSED_WIDTH = 36
    EXPANDED_WIDTH = 158
    BUTTON_HEIGHT = 36
    ICON_SIZE = 18

    def __init__(
        self,
        symbol: str,
        label: str,
        parent=None,
        *,
        role: str | None = None,
        mode_type: str | None = None,
        icon_path: str | Path | None = None,
        icon_color: str | QtGui.QColor | None = None,
    ):
        super().__init__(symbol, parent)
        self._symbol = symbol
        self._label = label
        self._icon_path = Path(icon_path) if icon_path else None
        self._icon_color = QtGui.QColor(icon_color) if icon_color else None
        self._reveal_progress = 0.0

        self.setObjectName("hoverRevealToolButton")
        self.setProperty("toolbarLabel", label)
        if role:
            self.setProperty("toolbarRole", role)
        if mode_type:
            self.setProperty("modeType", mode_type)
            self.setCheckable(True)
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.setMouseTracking(True)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.setAccessibleName(label)
        self.setToolTip(label)
        self.setFixedSize(self.EXPANDED_WIDTH, self.BUTTON_HEIGHT)

        self._reveal_label = QtWidgets.QLabel(label, self)
        self._reveal_label.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self._reveal_label.setAlignment(QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft)
        self._reveal_label.setVisible(False)

        self._label_opacity = QtWidgets.QGraphicsOpacityEffect(self._reveal_label)
        self._label_opacity.setOpacity(0.0)
        self._reveal_label.setGraphicsEffect(self._label_opacity)

        self._animation = QtCore.QPropertyAnimation(self, b"revealProgress", self)
        self._animation.setDuration(160)
        self._animation.setEasingCurve(QtCore.QEasingCurve.OutCubic)
        self._sync_label_geometry()

    def revealProgress(self) -> float:
        return self._reveal_progress

    def setRevealProgress(self, value: float) -> None:
        self._reveal_progress = max(0.0, min(1.0, float(value)))
        self._sync_label_geometry()
        self.update()

    revealProgress = QtCore.Property(float, revealProgress, setRevealProgress)

    def revealExtent(self) -> float:
        return self.COLLAPSED_WIDTH + (self.EXPANDED_WIDTH - self.COLLAPSED_WIDTH) * self._reveal_progress

    def enterEvent(self, event: QtCore.QEvent) -> None:
        self._animate_reveal(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event: QtCore.QEvent) -> None:
        self._animate_reveal(0.0)
        super().leaveEvent(event)

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)

        theme = get_theme()
        colors = theme.tokens.get("colors", {})
        active = bool(self.property("modeActive")) or self.isChecked()
        hovered = self.underMouse() or self._reveal_progress > 0.0
        role = self.property("toolbarRole")
        mode_type = self.property("modeType")

        bg = QtGui.QColor(colors.get("surface_alt", "#2b2b2b"))
        bg.setAlpha(232)
        border = QtGui.QColor(colors.get("border", "#444444"))
        if hovered:
            bg = QtGui.QColor(colors.get("accent_hover", "#4b4b4b"))
            border = QtGui.QColor(colors.get("accent_soft", "#ff5f5f"))
        if role == "close" and hovered:
            bg = QtGui.QColor(colors.get("accent", "#c2181f"))
            border = bg
        if active and mode_type:
            color_key = {
                "area": "mode_area",
                "resize": "mode_resize",
                "delete": "accent",
            }.get(str(mode_type), "accent")
            bg = QtGui.QColor(colors.get(color_key, colors.get("accent", "#c2181f")))
            border = bg

        width = self.revealExtent()
        rect = QtCore.QRectF(0.5, 0.5, width - 1.0, self.BUTTON_HEIGHT - 1.0)
        radius = self.BUTTON_HEIGHT / 2
        painter.setPen(QtGui.QPen(border, 1))
        painter.setBrush(QtGui.QBrush(bg))
        painter.drawRoundedRect(rect, radius, radius)

        label_color = self._label_color()
        self._paint_symbol(painter, self._symbol_color(label_color))
        self._reveal_label.setStyleSheet(f"color: {label_color.name()}; background: transparent;")

    def _animate_reveal(self, target: float) -> None:
        self._animation.stop()
        self._animation.setStartValue(self._reveal_progress)
        self._animation.setEndValue(target)
        self._animation.start()

    def _sync_label_geometry(self) -> None:
        slide_offset = int((1.0 - self._reveal_progress) * -6)
        self._reveal_label.setGeometry(
            self.COLLAPSED_WIDTH + slide_offset,
            0,
            self.EXPANDED_WIDTH - self.COLLAPSED_WIDTH - 10,
            self.BUTTON_HEIGHT,
        )
        visible = self._reveal_progress > 0.02
        self._reveal_label.setVisible(visible)
        self._label_opacity.setOpacity(self._reveal_progress)

    def _label_color(self) -> QtGui.QColor:
        if self.property("modeType") or self.property("toolbarRole") == "close":
            return QtGui.QColor("#ffffff")
        return QtGui.QColor(get_theme().tokens.get("colors", {}).get("text", "#f5f5f5"))

    def _symbol_color(self, fallback_color: QtGui.QColor) -> QtGui.QColor:
        if self.property("modeType") and (bool(self.property("modeActive")) or self.isChecked()):
            return QtGui.QColor("#ffffff")
        return self._icon_color or fallback_color

    def _paint_symbol(self, painter: QtGui.QPainter, color: QtGui.QColor) -> None:
        if self._icon_path and self._icon_path.exists():
            if self._paint_svg_icon(painter, self._icon_path, color):
                return

        painter.setPen(QtGui.QPen(color, 2))
        painter.setBrush(QtCore.Qt.NoBrush)
        painter.setFont(QtGui.QFont(self.font().family(), 12, QtGui.QFont.Bold))
        symbol_rect = QtCore.QRectF(0, 0, self.COLLAPSED_WIDTH, self.BUTTON_HEIGHT)
        painter.drawText(symbol_rect, QtCore.Qt.AlignCenter, self._symbol)

    def _paint_svg_icon(
        self,
        painter: QtGui.QPainter,
        icon_path: Path,
        color: QtGui.QColor,
    ) -> bool:
        renderer = QtSvg.QSvgRenderer(str(icon_path))
        if not renderer.isValid():
            return False

        size = min(self.ICON_SIZE, self.BUTTON_HEIGHT - 6, self.COLLAPSED_WIDTH - 6)
        x = int((self.COLLAPSED_WIDTH - size) / 2)
        y = int((self.BUTTON_HEIGHT - size) / 2)

        pixmap = QtGui.QPixmap(size, size)
        pixmap.fill(QtCore.Qt.transparent)

        svg_painter = QtGui.QPainter(pixmap)
        svg_painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        renderer.render(svg_painter, QtCore.QRectF(0, 0, size, size))
        svg_painter.setCompositionMode(QtGui.QPainter.CompositionMode_SourceIn)
        svg_painter.fillRect(pixmap.rect(), color)
        svg_painter.end()

        painter.drawPixmap(x, y, pixmap)
        return True


class PDFGraphicsView(QtWidgets.QGraphicsView):
    DELETE_CURSOR_LOGICAL_SIZE = 32

    def __init__(self, parent_viewer: "PDFViewer"):
        super().__init__(parent_viewer)
        self.parent_viewer = parent_viewer
        self.setRenderHints(QtGui.QPainter.Antialiasing | QtGui.QPainter.SmoothPixmapTransform)
        self.setDragMode(QtWidgets.QGraphicsView.NoDrag)
        self.setCursor(QtCore.Qt.ArrowCursor)
        self.setAcceptDrops(True)
        self.setViewportUpdateMode(QtWidgets.QGraphicsView.FullViewportUpdate)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self._dragging_rect = False
        self._dragging_move = False
        self._dragging_resize = False
        self._start_pos = None
        self._last_edit_pos: QtCore.QPointF | None = None
        self._active_resize_handle: str | None = None
        self._delete_cursor = self._build_delete_cursor()

    def _build_delete_cursor(self) -> QtGui.QCursor:
        icon_path = Path(__file__).resolve().parent / "style" / "icons" / "delete.svg"
        try:
            svg_data = icon_path.read_text(encoding="utf-8").replace(
                "currentColor", "#d32f2f"
            )
        except OSError:
            return QtGui.QCursor(QtCore.Qt.PointingHandCursor)

        renderer = QtSvg.QSvgRenderer(QtCore.QByteArray(svg_data.encode("utf-8")))
        if not renderer.isValid():
            return QtGui.QCursor(QtCore.Qt.PointingHandCursor)

        logical_size = self.DELETE_CURSOR_LOGICAL_SIZE
        device_ratio = max(1.0, self.devicePixelRatioF())
        physical_size = max(logical_size, round(logical_size * device_ratio))
        pixmap = QtGui.QPixmap(physical_size, physical_size)
        pixmap.setDevicePixelRatio(device_ratio)
        pixmap.fill(QtCore.Qt.transparent)

        painter = QtGui.QPainter(pixmap)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        renderer.render(painter, QtCore.QRectF(9.0, 1.0, 21.0, 21.0))

        hotspot = QtCore.QPointF(5.0, 26.0)
        target_lines = (
            QtCore.QLineF(1.0, hotspot.y(), 9.0, hotspot.y()),
            QtCore.QLineF(hotspot.x(), 22.0, hotspot.x(), 30.0),
        )
        painter.setPen(QtGui.QPen(QtGui.QColor("#ffffff"), 4.0))
        painter.drawLines(target_lines)
        painter.setPen(QtGui.QPen(QtGui.QColor("#d32f2f"), 2.0))
        painter.drawLines(target_lines)
        painter.end()

        return QtGui.QCursor(pixmap, round(hotspot.x()), round(hotspot.y()))

    def dragEnterEvent(self, event: QtGui.QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event: QtGui.QDragMoveEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event: QtGui.QDropEvent) -> None:
        urls = event.mimeData().urls()
        if not urls:
            return
        paths = [url.toLocalFile() for url in urls if url.isLocalFile()]
        if not paths:
            return

        pdf_paths = [
            path for path in paths
            if path.lower().endswith(".pdf") and os.path.isfile(path)
        ]
        if pdf_paths:
            self.parent_viewer.parent.drop_pdf_folder(pdf_paths)
            event.acceptProposedAction()

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.LeftButton:
            pos = self.mapToScene(event.position().toPoint())
            mode = self.parent_viewer.selection_mode
            if mode == "area" and self.parent_viewer.has_document:
                self._dragging_rect = True
                self._start_pos = pos
                self.parent_viewer.start_rectangle(self._start_pos)
                self.refresh_interaction_cursor(pos)
            elif mode == "resize":
                handle = self.parent_viewer.resize_handle_at_scene_pos(pos)
                if handle and self.parent_viewer.select_resize_handle_target():
                    self._dragging_resize = True
                    self._active_resize_handle = handle
                    self.parent_viewer.begin_geometry_edit_drag("resize")
                    self.parent_viewer.set_active_resize_handle(handle)
                    self.refresh_interaction_cursor(pos)
                else:
                    if self.parent_viewer.select_rectangle_at_scene_pos(pos):
                        self._dragging_move = True
                        self._last_edit_pos = pos
                        self.parent_viewer.begin_geometry_edit_drag("move")
                        self.refresh_interaction_cursor(pos)
                    else:
                        self.parent_viewer.clear_rectangle_selection()
                        self._update_hover_cursor(pos)
            elif mode == "delete":
                self.parent_viewer.delete_rectangle_at_scene_pos(pos)
            event.accept()
            return

        if event.button() == QtCore.Qt.RightButton:
            self.parent_viewer.show_context_menu(event)
            event.accept()
            return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        if self._dragging_rect:
            pos = self.mapToScene(event.position().toPoint())
            self.parent_viewer.draw_rectangle(pos)
            self.parent_viewer.auto_scroll_canvas(event.position().toPoint())
            event.accept()
            return
        if self._dragging_move and self._last_edit_pos is not None:
            pos = self.mapToScene(event.position().toPoint())
            self.parent_viewer.move_selected_rectangle_by_scene_delta(pos - self._last_edit_pos)
            self._last_edit_pos = pos
            self.parent_viewer.auto_scroll_canvas(event.position().toPoint())
            event.accept()
            return
        if self._dragging_resize and self._active_resize_handle:
            pos = self.mapToScene(event.position().toPoint())
            self.parent_viewer.resize_selected_rectangle_to_scene_pos(self._active_resize_handle, pos)
            self.parent_viewer.auto_scroll_canvas(event.position().toPoint())
            event.accept()
            return
        self._update_hover_cursor(self.mapToScene(event.position().toPoint()))
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        if self._dragging_rect and event.button() == QtCore.Qt.LeftButton:
            pos = self.mapToScene(event.position().toPoint())
            self.parent_viewer.end_rectangle(pos)
            self._dragging_rect = False
            self._start_pos = None
            self.refresh_interaction_cursor(pos)
            event.accept()
            return
        if event.button() == QtCore.Qt.LeftButton and (self._dragging_move or self._dragging_resize):
            self._dragging_move = False
            self._dragging_resize = False
            self._last_edit_pos = None
            self._active_resize_handle = None
            self.parent_viewer.set_active_resize_handle(None)
            self.parent_viewer.finish_geometry_edit_drag()
            self._update_hover_cursor(self.mapToScene(event.position().toPoint()))
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:
        if event.key() == QtCore.Qt.Key.Key_Delete:
            self.parent_viewer.delete_selected_rectangle()
            event.accept()
            return
        super().keyPressEvent(event)

    def _update_hover_cursor(self, scene_pos: QtCore.QPointF) -> None:
        self.refresh_interaction_cursor(scene_pos)

    def _default_cursor_for_mode(self) -> QtCore.Qt.CursorShape:
        if self.parent_viewer.selection_mode == "area":
            return QtCore.Qt.CrossCursor
        return QtCore.Qt.ArrowCursor

    def _cursor_for_scene_pos(
        self,
        scene_pos: QtCore.QPointF,
    ) -> QtCore.Qt.CursorShape | QtGui.QCursor:
        if self._dragging_rect:
            return QtCore.Qt.CrossCursor
        if self._dragging_resize and self._active_resize_handle:
            return self.parent_viewer.cursor_for_resize_handle(
                self._active_resize_handle
            )
        if self._dragging_move:
            return QtCore.Qt.SizeAllCursor

        mode = self.parent_viewer.selection_mode
        if mode == "area":
            self.parent_viewer.set_hovered_resize_handle(None)
            self.parent_viewer.set_hover_rectangle_at_scene_pos(scene_pos)
            return QtCore.Qt.CrossCursor

        if mode == "resize":
            handle = self.parent_viewer.resize_handle_at_scene_pos(scene_pos)
            self.parent_viewer.set_hovered_resize_handle(handle)
            if handle:
                return self.parent_viewer.cursor_for_resize_handle(handle)
            if self.parent_viewer.set_hover_rectangle_at_scene_pos(scene_pos):
                handle = self.parent_viewer.resize_handle_at_scene_pos(scene_pos)
                self.parent_viewer.set_hovered_resize_handle(handle)
                if handle:
                    return self.parent_viewer.cursor_for_resize_handle(handle)
                return QtCore.Qt.SizeAllCursor
            return QtCore.Qt.ArrowCursor

        self.parent_viewer.set_hovered_resize_handle(None)
        hit = self.parent_viewer.rectangle_at_scene_pos(scene_pos)
        if hit is None:
            self.parent_viewer.clear_hover_rectangle()
            return QtCore.Qt.ArrowCursor

        self.parent_viewer._set_hover_rectangle(hit)
        if mode == "delete":
            return self._delete_cursor
        return self._default_cursor_for_mode()

    def refresh_interaction_cursor(
        self,
        scene_pos: QtCore.QPointF | None = None,
    ) -> None:
        if scene_pos is None:
            viewport_pos = self.viewport().mapFromGlobal(QtGui.QCursor.pos())
            if not self.viewport().rect().contains(viewport_pos):
                self._set_view_cursor(self._default_cursor_for_mode())
                return
            scene_pos = self.mapToScene(viewport_pos)
        self._set_view_cursor(self._cursor_for_scene_pos(scene_pos))

    def enterEvent(self, event: QtCore.QEvent) -> None:
        self.refresh_interaction_cursor()
        super().enterEvent(event)

    def leaveEvent(self, event: QtCore.QEvent) -> None:
        self._clear_target_cursor()
        super().leaveEvent(event)

    def viewportEvent(self, event: QtCore.QEvent) -> bool:
        handled = super().viewportEvent(event)
        if event.type() == QtCore.QEvent.Type.Enter:
            self.refresh_interaction_cursor()
        elif event.type() == QtCore.QEvent.Type.Leave:
            self._clear_target_cursor()
        return handled

    def _clear_target_cursor(self) -> None:
        self.parent_viewer.set_hovered_resize_handle(None)
        self.parent_viewer.clear_hover_rectangle()
        self._set_view_cursor(self._default_cursor_for_mode())

    def reset_interaction_cursor_state(self) -> None:
        self._dragging_rect = False
        self._dragging_move = False
        self._dragging_resize = False
        self._start_pos = None
        self._last_edit_pos = None
        self._active_resize_handle = None
        self.parent_viewer.set_active_resize_handle(None)
        self.parent_viewer.set_hovered_resize_handle(None)
        self.refresh_interaction_cursor()

    def _set_view_cursor(
        self,
        cursor: QtCore.Qt.CursorShape | QtGui.QCursor,
    ) -> None:
        self.setCursor(cursor)
        self.viewport().setCursor(cursor)

    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:
        modifiers = event.modifiers()

        if modifiers & QtCore.Qt.ControlModifier:
            delta = 0.1 if event.angleDelta().y() > 0 else -0.1
            self.parent_viewer.set_zoom(max(0.1, min(4.0, self.parent_viewer.current_zoom + delta)))
            self.parent_viewer.parent.update_zoom_slider(self.parent_viewer.current_zoom)
            event.accept()
            return

        if modifiers & QtCore.Qt.ShiftModifier:
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - event.angleDelta().y())
            event.accept()
            return

        super().wheelEvent(event)


class PDFViewer(QtWidgets.QWidget):
    RESIZE_HANDLE_NAMES = (
        "top_left",
        "top",
        "top_right",
        "right",
        "bottom_right",
        "bottom",
        "bottom_left",
        "left",
    )
    _CORNER_CURSORS = {
        "top_left": QtCore.Qt.SizeFDiagCursor,
        "bottom_right": QtCore.Qt.SizeFDiagCursor,
        "top_right": QtCore.Qt.SizeBDiagCursor,
        "bottom_left": QtCore.Qt.SizeBDiagCursor,
        "left": QtCore.Qt.SizeHorCursor,
        "right": QtCore.Qt.SizeHorCursor,
        "top": QtCore.Qt.SizeVerCursor,
        "bottom": QtCore.Qt.SizeVerCursor,
    }
    MIN_RECTANGLE_SCENE_SIZE = 3.0
    HANDLE_SCENE_SIZE = 8.0

    def __init__(self, parent, master=None, pdf_backend: PdfBackend | None = None):
        super().__init__(master)
        self.parent = parent
        self._next_editor_id = 1
        self.edit_history = ViewerEditHistory(self._apply_viewer_edit)
        self._geometry_drag: tuple[str, Coordinates, str] | None = None
        shortcut_context = QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut
        self.undo_shortcut = QtGui.QShortcut(
            QtGui.QKeySequence(QtGui.QKeySequence.StandardKey.Undo), self
        )
        self.undo_shortcut.setContext(shortcut_context)
        self.undo_shortcut.activated.connect(self._handle_undo_shortcut)
        self.redo_shortcut = QtGui.QShortcut(
            QtGui.QKeySequence(QtGui.QKeySequence.StandardKey.Redo), self
        )
        self.redo_shortcut.setContext(shortcut_context)
        self.redo_shortcut.activated.connect(self._handle_redo_shortcut)
        self.alternate_redo_shortcut = QtGui.QShortcut(
            QtGui.QKeySequence("Ctrl+Shift+Z"), self
        )
        self.alternate_redo_shortcut.setContext(shortcut_context)
        self.alternate_redo_shortcut.activated.connect(self._handle_redo_shortcut)
        self._pdf_backend = pdf_backend or create_pdf_backend()
        theme = get_theme()

        self.scene = QtWidgets.QGraphicsScene(self)
        self.view = PDFGraphicsView(self)
        self.view.setScene(self.scene)

        canvas_color = theme.tokens.get("colors", {}).get("canvas", "#1e1e1e")
        self.view.setBackgroundBrush(QtGui.QBrush(QtGui.QColor(canvas_color)))

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view)

        self.empty_overlay = QtWidgets.QLabel(
            "Open a PDF to draw extraction areas",
            self,
        )
        self.empty_overlay.setObjectName("emptyOverlay")
        self.empty_overlay.setAlignment(QtCore.Qt.AlignCenter)
        self.empty_overlay.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents)

        self.current_zoom = CURRENT_ZOOM

        # ── top-left controls (recent / close) ────────────────────────────────
        self.controls_container = QtWidgets.QWidget(self)
        self.controls_container.setObjectName("viewerControls")
        controls_layout = QtWidgets.QHBoxLayout(self.controls_container)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(int(theme.tokens.get("spacing", {}).get("xs", 4)))

        self.recent_pdf_button = QtWidgets.QPushButton("⟲")
        self.recent_pdf_button.setObjectName("viewerControlButton")
        self.recent_pdf_button.clicked.connect(self.parent.open_recent_pdf)

        self.close_pdf_button = QtWidgets.QPushButton("×")
        self.close_pdf_button.setObjectName("viewerCloseButton")
        self.close_pdf_button.clicked.connect(self.parent.close_pdf)

        controls_layout.addWidget(self.recent_pdf_button)
        controls_layout.addWidget(self.close_pdf_button)

        self.canvas_toolbar = QtWidgets.QWidget(self)
        self.canvas_toolbar.setObjectName("viewerCanvasToolbar")
        self.canvas_toolbar.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        toolbar_layout = QtWidgets.QVBoxLayout(self.canvas_toolbar)
        toolbar_layout.setContentsMargins(0, 0, 0, 0)
        toolbar_layout.setSpacing(int(theme.tokens.get("spacing", {}).get("sm", 6)))

        icon_dir = Path(__file__).resolve().parent / "style" / "icons"
        self.close_pdf_button = HoverRevealToolButton(
            "x",
            "Close PDF View",
            self.canvas_toolbar,
            role="close",
        )
        self.mode_area_btn = HoverRevealToolButton(
            "",
            "Extract Text",
            self.canvas_toolbar,
            mode_type="area",
            icon_path=icon_dir / "tab-rectangles.svg",
            icon_color="red",
        )
        self.mode_resize_btn = HoverRevealToolButton(
            "",
            "Resize",
            self.canvas_toolbar,
            mode_type="resize",
            icon_path=icon_dir / "resize.svg",
        )
        self.mode_delete_btn = HoverRevealToolButton(
            "",
            "Delete",
            self.canvas_toolbar,
            mode_type="delete",
            icon_path=icon_dir / "delete.svg",
        )
        self.mode_delete_btn.setAccessibleName("Delete areas")
        self.mode_delete_btn.setToolTip("Delete an area; use Ctrl+Z to undo")

        self.close_pdf_button.clicked.connect(self.parent.close_pdf)
        self.mode_area_btn.clicked.connect(lambda: self.set_interaction_mode("area"))
        self.mode_resize_btn.clicked.connect(lambda: self.set_interaction_mode("resize"))
        self.mode_delete_btn.clicked.connect(lambda: self.set_interaction_mode("delete"))

        for button in (
            self.close_pdf_button,
            self.mode_area_btn,
            self.mode_resize_btn,
            self.mode_delete_btn,
        ):
            toolbar_layout.addWidget(button)

        # ── zoom bar (pill) ───────────────────────────────────────────────────
        self.zoom_container = PillBar(self)
        self.zoom_container.setFixedHeight(34)

        bar_h = 34
        self.zoom_container.setFixedHeight(bar_h)

        zoom_layout = QtWidgets.QHBoxLayout(self.zoom_container)

        # use REAL layout margins (QSS padding doesn't inset children)
        pad_x = int(theme.tokens.get("spacing", {}).get("md", 10))
        pad_y = int(theme.tokens.get("spacing", {}).get("sm", 6))
        zoom_layout.setContentsMargins(pad_x, pad_y, pad_x, pad_y)
        zoom_layout.setSpacing(int(theme.tokens.get("spacing", {}).get("sm", 6)))

        self.page_navigator = QtWidgets.QWidget(self.zoom_container)
        self.page_navigator.setObjectName("viewerPageNavigator")
        self.page_navigator.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Fixed,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        page_layout = QtWidgets.QHBoxLayout(self.page_navigator)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(int(theme.tokens.get("spacing", {}).get("xs", 4)))

        self.previous_page_button = QtWidgets.QPushButton("‹", self.page_navigator)
        self.previous_page_button.setObjectName("viewerPagePreviousButton")
        self.previous_page_button.setAccessibleName("Previous page")
        self.previous_page_button.setToolTip("Go to the previous PDF page")
        self.previous_page_button.setFixedSize(22, 22)

        self.page_input = PageNumberSpinBox(self.page_navigator)
        self.page_input.setObjectName("viewerPageInput")
        self.page_input.setAccessibleName("Current page")
        self.page_input.setToolTip("Enter a PDF page number")
        self.page_input.setButtonSymbols(QtWidgets.QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.page_input.setKeyboardTracking(False)
        self.page_input.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.page_input.setFixedSize(46, 22)
        self.page_input.setRange(0, 0)

        self.page_divider_label = QtWidgets.QLabel("/", self.page_navigator)
        self.page_divider_label.setObjectName("viewerPageDividerLabel")
        self.page_divider_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.page_divider_label.setFixedWidth(8)

        self.page_total_label = QtWidgets.QLabel("0", self.page_navigator)
        self.page_total_label.setObjectName("viewerPageTotalLabel")
        self.page_total_label.setAccessibleName("Total pages")
        self.page_total_label.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter
        )
        self.page_total_label.setFixedWidth(34)

        self.next_page_button = QtWidgets.QPushButton("›", self.page_navigator)
        self.next_page_button.setObjectName("viewerPageNextButton")
        self.next_page_button.setAccessibleName("Next page")
        self.next_page_button.setToolTip("Go to the next PDF page")
        self.next_page_button.setFixedSize(22, 22)

        page_layout.addWidget(self.previous_page_button)
        page_layout.addWidget(self.page_input)
        page_layout.addWidget(self.page_divider_label)
        page_layout.addWidget(self.page_total_label)
        page_layout.addWidget(self.next_page_button)
        zoom_layout.addWidget(self.page_navigator, 0, QtCore.Qt.AlignmentFlag.AlignVCenter)

        self.page_navigator_separator = QtWidgets.QFrame(self.zoom_container)
        self.page_navigator_separator.setObjectName("viewerPageNavigatorSeparator")
        self.page_navigator_separator.setFrameShape(QtWidgets.QFrame.Shape.VLine)
        self.page_navigator_separator.setFixedSize(1, 18)
        zoom_layout.addWidget(
            self.page_navigator_separator,
            0,
            QtCore.Qt.AlignmentFlag.AlignVCenter,
        )

        self.zoom_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.zoom_slider.setObjectName("viewerZoomSlider")
        self.zoom_slider.setRange(10, 400)
        self.zoom_slider.setValue(int(self.current_zoom * 100))
        zoom_layout.addWidget(self.zoom_slider, 1)

        text_color = QtGui.QColor(theme.tokens.get("colors", {}).get("text", "#ffffff"))
        self.zoom_icon = MagnifierIcon(self.zoom_container, color=text_color)
        zoom_layout.addWidget(self.zoom_icon, 0, QtCore.Qt.AlignVCenter)

        # ── pdf state ─────────────────────────────────────────────────────────
        self.pdf_document: PdfDocumentSession | None = None
        self.current_pdf_path: str | None = None
        self._pdf_bytes: bytes | None = None
        self.current_page_number = 0
        self.pixmap_item: QtWidgets.QGraphicsPixmapItem | None = None
        self.pdf_width = 0
        self.pdf_height = 0
        self._coordinate_basis: CoordinateBasis | None = None
        self._current_page_basis: CoordinateBasis | None = None

        self.previous_page_button.clicked.connect(
            lambda _checked=False: self.set_current_page_index(
                self.current_page_number - 1
            )
        )
        self.next_page_button.clicked.connect(
            lambda _checked=False: self.set_current_page_index(
                self.current_page_number + 1
            )
        )
        self.page_input.editingFinished.connect(self._commit_page_input)
        self._sync_page_navigator()

        # rectangles
        self.areas: list[AreaSpec] = []
        self._area_editor_ids: list[str] = []
        self.rectangle_items: list[QtWidgets.QGraphicsRectItem] = []
        self.current_rectangle: QtWidgets.QGraphicsRectItem | None = None
        self.original_coordinates: list[float] | None = None
        self.canvas_image = None
        self.selected_rectangle_index: int | None = None
        self.hover_rectangle_index: int | None = None
        self.resize_handles: dict[str, QtWidgets.QGraphicsRectItem] = {}
        self._hovered_resize_handle: str | None = None
        self._active_resize_handle: str | None = None

        self.selection_mode = "area"
        self._scroll_counter = 0

        self.set_canvas_mode_button_states(self.selection_mode)
        self._set_empty_state_visible(True)
        self.set_viewer_controls_state(False, False)
        self._position_controls()

        # Keep the deferred layout callback owned by the viewer so destruction
        # cancels it before child widgets disappear.
        self._layout_timer = QtCore.QTimer(self)
        self._layout_timer.setSingleShot(True)
        self._layout_timer.timeout.connect(self._after_first_layout)
        self._layout_timer.start(0)

    @property
    def pdf_backend(self) -> PdfBackend:
        """Return the concrete backend used by this viewer."""
        return self._pdf_backend

    @property
    def current_page_size(self) -> tuple[float, float] | None:
        """Return the current page dimensions, or ``None`` without a document."""
        document = self.pdf_document
        if document is None:
            return None
        page = self._pdf_backend.documents.page_info(
            document,
            self.current_page_number,
        )
        return float(page.width), float(page.height)

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        super().showEvent(event)
        # ✅ Some layouts finalize at show; refresh once again
        self._layout_timer.start(0)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        self._layout_timer.stop()
        self.edit_history.clear()
        super().closeEvent(event)

    def _after_first_layout(self) -> None:
        self._refresh_empty_state_geometry()
        self._position_controls()
        # stack: view (bottom) -> overlay -> controls/zoom (top)
        if self.empty_overlay.isVisible():
            self.empty_overlay.raise_()
        self.controls_container.raise_()
        self.canvas_toolbar.raise_()
        self.zoom_container.raise_()

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        self._refresh_empty_state_geometry()

        # stack order on resize too
        if self.empty_overlay.isVisible():
            self.empty_overlay.raise_()

        self._position_controls()

    def _set_empty_state_visible(self, show: bool):
        self.empty_overlay.setVisible(show)
        self._refresh_empty_state_geometry()
        if show:
            self.empty_overlay.raise_()
            self.controls_container.raise_()
            self.canvas_toolbar.raise_()
            self.zoom_container.raise_()

    def _refresh_empty_state_geometry(self):
        if not self.empty_overlay.isVisible():
            return

        # ✅ cover the *viewport* (not the whole widget) so it is always centered correctly
        vp = self.view.viewport()
        top_left = vp.mapTo(self, QtCore.QPoint(0, 0))
        self.empty_overlay.setGeometry(QtCore.QRect(top_left, vp.size()))

    def _position_controls(self) -> None:
        tokens = get_theme().tokens
        padding = int(tokens.get("spacing", {}).get("lg", 12))
        scrollbar_size = int(tokens.get("sizes", {}).get("scrollbar", 12))
        offset = scrollbar_size + int(tokens.get("spacing", {}).get("xs", 4))
        available_width = max(0, self.width() - (2 * padding))

        self.controls_container.adjustSize()
        self.controls_container.move(padding, padding)
        self.controls_container.setVisible(not self.recent_pdf_button.isHidden())
        self.controls_container.raise_()

        self.canvas_toolbar.adjustSize()
        self.canvas_toolbar.move(padding, padding)
        self.canvas_toolbar.raise_()

        zoom_width = min(self.zoom_container.sizeHint().width(), available_width)
        self.zoom_container.setFixedWidth(zoom_width)
        self.zoom_container.adjustSize()
        zoom_size = self.zoom_container.size()
        zoom_x = padding
        zoom_y = max(padding, self.height() - zoom_size.height() - padding - offset)
        self.zoom_container.move(zoom_x, zoom_y)
        self.zoom_container.raise_()

    def set_viewer_controls_state(self, has_pdf: bool, has_recent: bool) -> None:
        self.canvas_toolbar.setVisible(has_pdf)
        self.zoom_container.setVisible(has_pdf)
        self.recent_pdf_button.setVisible(not has_pdf and has_recent)
        for button in self.controls_container.findChildren(QtWidgets.QPushButton):
            if button is not self.recent_pdf_button:
                button.setVisible(False)
        self._position_controls()

    def set_current_page_size(
        self,
        width: float | None,
        height: float | None,
    ) -> None:
        self._current_page_basis = (
            CoordinateBasis(float(width), float(height))
            if width is not None and height is not None
            else None
        )
        if self.areas:
            self.update_rectangles()
        self._position_controls()

    def set_coordinate_basis(
        self,
        width: float | None,
        height: float | None,
    ) -> None:
        basis = (
            CoordinateBasis(float(width), float(height))
            if width is not None and height is not None
            else None
        )
        if basis == self._coordinate_basis:
            return
        self._coordinate_basis = basis
        if self.areas:
            self.update_rectangles()

    @property
    def coordinate_basis(self) -> CoordinateBasis | None:
        return self._coordinate_basis

    def _coordinate_transform(self) -> CoordinateTransform | None:
        if self._coordinate_basis is None or self._current_page_basis is None:
            return None
        return CoordinateTransform(self._coordinate_basis, self._current_page_basis)

    def _canonical_to_page_rect(self, coordinates) -> tuple[float, float, float, float]:
        transform = self._coordinate_transform()
        values = tuple(float(value) for value in coordinates)
        return transform.to_target_rect(values) if transform is not None else values

    def _page_to_canonical_rect(self, coordinates) -> tuple[float, float, float, float]:
        transform = self._coordinate_transform()
        values = tuple(float(value) for value in coordinates)
        return transform.to_source_rect(values) if transform is not None else values

    def set_canvas_mode_button_states(self, active_mode: str) -> None:
        for button in (
            self.mode_area_btn,
            self.mode_resize_btn,
            self.mode_delete_btn,
        ):
            active = button.property("modeType") == active_mode
            button.setProperty("modeActive", active)
            button.setChecked(active)
            button.style().unpolish(button)
            button.style().polish(button)
            button.update()

    def set_interaction_mode(self, mode: str) -> None:
        if mode not in {"area", "resize", "delete"}:
            raise ValueError(f"Unsupported rectangle interaction mode: {mode}")
        self.selection_mode = mode
        self.set_canvas_mode_button_states(mode)
        self.current_rectangle = None
        self.original_coordinates = None
        self.clear_hover_rectangle()
        self._hovered_resize_handle = None
        self._active_resize_handle = None
        if mode == "area":
            self.clear_rectangle_selection()
        else:
            self._refresh_selection_visuals()
        self.view.refresh_interaction_cursor()

    def _sync_page_navigator(self) -> None:
        document = self.pdf_document
        page_count = max(0, int(document.page_count)) if document is not None else 0
        blocker = QtCore.QSignalBlocker(self.page_input)
        try:
            if page_count:
                current_index = min(max(0, self.current_page_number), page_count - 1)
                self.page_input.setRange(1, page_count)
                self.page_input.setValue(current_index + 1)
                self.page_total_label.setText(str(page_count))
                self.previous_page_button.setEnabled(current_index > 0)
                self.next_page_button.setEnabled(current_index < page_count - 1)
            else:
                self.page_input.setRange(0, 0)
                self.page_input.setValue(0)
                self.page_total_label.setText("0")
                self.previous_page_button.setEnabled(False)
                self.next_page_button.setEnabled(False)
        finally:
            del blocker

    def _commit_page_input(self) -> None:
        raw_value = self.page_input.lineEdit().text().strip()
        document = self.pdf_document
        page_count = max(0, int(document.page_count)) if document is not None else 0
        if not raw_value or not raw_value.isascii() or not raw_value.isdecimal():
            self._sync_page_navigator()
            return
        page_number = int(raw_value)
        if not 1 <= page_number <= page_count:
            self._sync_page_navigator()
            return
        self.set_current_page_index(page_number - 1)

    def _close_document(self, *, clear_edit_history: bool) -> None:
        if clear_edit_history:
            self.edit_history.clear()
        self._geometry_drag = None
        self.scene.clear()
        self.pixmap_item = None
        self._reset_rectangle_state()
        self.view.reset_interaction_cursor_state()

        document = self.pdf_document
        self.pdf_document = None
        self.current_pdf_path = None
        self.set_current_page_size(None, None)
        self._pdf_bytes = None
        self.current_page_number = 0
        self.pdf_width = 0
        self.pdf_height = 0
        self._sync_page_navigator()

        if document:
            try:
                document.close()
            except Exception as exc:
                LOGGER.warning(
                    "PDF viewer document cleanup failed (%s)",
                    type(exc).__name__,
                )

        self._set_empty_state_visible(True)
        try:
            self.parent.on_pdf_closed()
        except Exception:
            pass

    def close_pdf(self) -> None:
        self._close_document(clear_edit_history=False)

    def display_pdf(self, pdf_path: str):
        if self.pdf_document:
            self._close_document(clear_edit_history=False)
        self._pdf_bytes = Path(pdf_path).read_bytes()
        try:
            self.pdf_document = self._pdf_backend.documents.open_bytes(
                self._pdf_bytes,
                name=pdf_path,
                normalize_rotation=False,
            )
        except EmptyDocumentError:
            # Legacy viewer behavior treated a valid zero-page PDF as an empty canvas.
            self._close_document(clear_edit_history=False)
            return
        self.current_pdf_path = pdf_path

        try:
            if self.pdf_document.page_count > 0:
                self.set_current_page_index(0, raise_on_failure=True)
                self._scroll_to_bottom_right()
                try:
                    self.parent.on_pdf_loaded(pdf_path)
                except Exception:
                    pass
            else:
                self._close_document(clear_edit_history=False)
        except Exception:
            self._close_document(clear_edit_history=False)
            raise

    @property
    def has_document(self) -> bool:
        return self.pdf_document is not None

    def _pixmap_for_page(self, page_index: int) -> QtGui.QPixmap:
        if self.pdf_document is None:
            return QtGui.QPixmap()
        image = self._pdf_backend.render.render_page(
            self.pdf_document,
            page_index,
            scale=self.current_zoom,
        )
        if image.pixel_format:
            formats = {
                "rgb888": QtGui.QImage.Format_RGB888,
                "rgba8888": QtGui.QImage.Format_RGBA8888,
            }
            qt_format = formats.get(image.pixel_format.lower())
            if qt_format is None or image.stride is None:
                raise ValueError("PDF backend returned an unsupported raw pixel format")
            qt_image = QtGui.QImage(
                image.data,
                image.width,
                image.height,
                image.stride,
                qt_format,
            ).copy()
            return QtGui.QPixmap.fromImage(qt_image)
        pixmap = QtGui.QPixmap()
        if pixmap.loadFromData(image.data):
            return pixmap
        raise ValueError("PDF backend returned image data that Qt could not decode")

    def _display_pixmap(self, pixmap: QtGui.QPixmap) -> None:
        if self.pixmap_item is None:
            self.scene.clear()
            self._reset_rectangle_state()
            self.pixmap_item = self.scene.addPixmap(pixmap)
        else:
            self.pixmap_item.setPixmap(pixmap)

        self.scene.setSceneRect(0, 0, pixmap.width(), pixmap.height())
        self._set_empty_state_visible(False)
        self.update_rectangles()
        self.view.refresh_interaction_cursor()

    def set_current_page_index(
        self,
        index: int,
        *,
        raise_on_failure: bool = False,
    ) -> bool:
        document = self.pdf_document
        if type(index) is not int or document is None:
            self._sync_page_navigator()
            return False

        page_count = max(0, int(document.page_count))
        if page_count == 0 or not 0 <= index < page_count:
            self._sync_page_navigator()
            return False
        if index == self.current_page_number and self.pixmap_item is not None:
            self._sync_page_navigator()
            return True

        try:
            page_info = self._pdf_backend.documents.page_info(document, index)
            pixmap = self._pixmap_for_page(index)
        except Exception:
            self._sync_page_navigator()
            if raise_on_failure:
                raise
            return False

        previous_index = self.current_page_number
        previous_width = self.pdf_width
        previous_height = self.pdf_height
        previous_basis = self._current_page_basis
        previous_pixmap = (
            self.pixmap_item.pixmap()
            if self.pixmap_item is not None
            else None
        )
        try:
            self.current_page_number = index
            self.pdf_width = int(page_info.width)
            self.pdf_height = int(page_info.height)
            self.set_current_page_size(page_info.width, page_info.height)
            self._display_pixmap(pixmap)
            self._sync_page_navigator()
            return True
        except Exception:
            self.current_page_number = previous_index
            self.pdf_width = previous_width
            self.pdf_height = previous_height
            try:
                if previous_basis is None:
                    self.set_current_page_size(None, None)
                else:
                    self.set_current_page_size(
                        previous_basis.width,
                        previous_basis.height,
                    )
                if previous_pixmap is not None:
                    self._display_pixmap(previous_pixmap)
            except Exception as exc:
                LOGGER.warning(
                    "PDF page navigation rollback failed (%s)",
                    type(exc).__name__,
                )
            self._sync_page_navigator()
            if raise_on_failure:
                raise
            return False

    def update_display(self):
        if not self.pdf_document:
            self.scene.clear()
            self.pixmap_item = None
            self._reset_rectangle_state()
            self._set_empty_state_visible(True)
            self.view.refresh_interaction_cursor()
            return

        pixmap = self._pixmap_for_page(self.current_page_number)
        self._display_pixmap(pixmap)

    def _scroll_to_bottom_right(self) -> None:
        def scroll():
            self.view.horizontalScrollBar().setValue(self.view.horizontalScrollBar().maximum())
            self.view.verticalScrollBar().setValue(self.view.verticalScrollBar().maximum())

        QtCore.QTimer.singleShot(0, self, scroll)

    def start_rectangle(self, pos: QtCore.QPointF):
        if self.selection_mode != "area":
            self.current_rectangle = None
            self.original_coordinates = None
            return
        color = QtGui.QColor("red")
        pen = QtGui.QPen(color, 2)
        self.original_coordinates = [pos.x(), pos.y()]
        self.current_rectangle = self.scene.addRect(QtCore.QRectF(pos, pos), pen)
        self.current_rectangle.setZValue(1)

    def draw_rectangle(self, pos: QtCore.QPointF):
        if self.current_rectangle and self.original_coordinates:
            rect = QtCore.QRectF(
                QtCore.QPointF(self.original_coordinates[0], self.original_coordinates[1]),
                pos,
            )
            self.current_rectangle.setRect(rect.normalized())

    def end_rectangle(self, pos: QtCore.QPointF):
        if not self.current_rectangle:
            return
        if self.selection_mode != "area":
            self._safe_remove_item(self.current_rectangle)
            self.current_rectangle = None
            self.original_coordinates = None
            return

        rect = self.current_rectangle.rect().normalized()
        x0, y0, x1, y1 = rect.left(), rect.top(), rect.right(), rect.bottom()
        page_coordinates = [
            x0 / self.current_zoom,
            y0 / self.current_zoom,
            x1 / self.current_zoom,
            y1 / self.current_zoom,
        ]
        adjusted_coordinates = list(self._page_to_canonical_rect(page_coordinates))

        current_rectangle = self.current_rectangle
        self.current_rectangle = None
        self.original_coordinates = None
        self._safe_remove_item(current_rectangle)

        existing_titles = {area.title for area in self.areas}
        number = 1
        while f"Rectangle {number}" in existing_titles:
            number += 1
        try:
            area_spec = AreaSpec.create(
                title=f"Rectangle {number}",
                rect=adjusted_coordinates,
            )
        except AreaSpecValidationError as exc:
            self._report_area_validation_error(exc)
            return
        area = AreaSnapshot.from_area_spec(
            area_spec,
            editor_id=self._new_editor_id(),
        )
        before_state = self._state_snapshot()
        if (
            not before_state.areas
            and before_state.coordinate_basis is None
            and self._current_page_basis is not None
        ):
            self.edit_history.execute(
                ViewerEdit.replace_state(
                    before_state,
                    ViewerStateSnapshot(
                        areas=(area,),
                        coordinate_basis=self._current_page_basis,
                    ),
                    description="Add area",
                )
            )
        else:
            self.edit_history.execute(ViewerEdit.add_area(area, len(self.areas)))

    def auto_scroll_canvas(self, pos: QtCore.QPoint):
        margin = SCROLL_MARGIN

        if self._scroll_counter < SCROLL_INCREMENT_THRESHOLD:
            self._scroll_counter += 1
            return
        self._scroll_counter = 0

        view_rect = self.view.viewport().rect()

        if pos.x() >= view_rect.width() - margin:
            self.view.horizontalScrollBar().setValue(self.view.horizontalScrollBar().value() + 10)
        elif pos.x() <= margin:
            self.view.horizontalScrollBar().setValue(self.view.horizontalScrollBar().value() - 10)

        if pos.y() >= view_rect.height() - margin:
            self.view.verticalScrollBar().setValue(self.view.verticalScrollBar().value() + 10)
        elif pos.y() <= margin:
            self.view.verticalScrollBar().setValue(self.view.verticalScrollBar().value() - 10)

    def update_rectangles(self):
        self._clear_resize_handles()
        for rect in self.rectangle_items:
            self._safe_remove_item(rect)
        self.rectangle_items.clear()


        for area in self.areas:
            page_rect = self._canonical_to_page_rect(area.rect)
            x0, y0, x1, y1 = [c * self.current_zoom for c in page_rect]
            rect_item = self.scene.addRect(
                QtCore.QRectF(x0, y0, x1 - x0, y1 - y0),
                QtGui.QPen(QtGui.QColor("red"), 2),
            )
            rect_item.setZValue(1)
            self.rectangle_items.append(rect_item)

        self._refresh_selection_visuals()
        self.parent.update_areas_treeview()

    def rectangle_at_scene_pos(self, pos: QtCore.QPointF, edge_tolerance: float = 0.0) -> int | None:
        for index in range(len(self.rectangle_items) - 1, -1, -1):
            item = self.rectangle_items[index]
            if item.sceneBoundingRect().adjusted(-edge_tolerance, -edge_tolerance, edge_tolerance, edge_tolerance).contains(pos):
                return index
        return None

    def select_rectangle_at_scene_pos(self, pos: QtCore.QPointF) -> bool:
        hit = self.rectangle_at_scene_pos(pos)
        if hit is None:
            return False
        self.selected_rectangle_index = hit
        self._refresh_selection_visuals()
        return True

    def clear_rectangle_selection(self) -> None:
        self.selected_rectangle_index = None
        self.hover_rectangle_index = None
        self._hovered_resize_handle = None
        self._active_resize_handle = None
        self._refresh_selection_visuals()

    def set_hover_rectangle_at_scene_pos(self, pos: QtCore.QPointF) -> bool:
        hit = self.rectangle_at_scene_pos(pos)
        if hit is None:
            self.clear_hover_rectangle()
            return False
        self._set_hover_rectangle(hit)
        return True

    def clear_hover_rectangle(self) -> None:
        self._set_hover_rectangle(None)

    def select_resize_handle_target(self) -> bool:
        if self._hover_rect_item() is not None:
            self.selected_rectangle_index = self.hover_rectangle_index
            self._refresh_selection_visuals()
        return self._selected_rect_item() is not None

    def begin_geometry_edit_drag(self, operation: str) -> None:
        target = self._selected_target()
        if target is None:
            self._geometry_drag = None
            return
        editor_id, coordinates = target
        self._geometry_drag = (editor_id, coordinates, operation)

    def finish_geometry_edit_drag(self) -> None:
        drag = self._geometry_drag
        self._geometry_drag = None
        if drag is None:
            return
        editor_id, before, operation = drag
        index = self._find_area_index(editor_id)
        if index is None:
            return
        after = self.areas[index].rect
        if before != after:
            description = "Resize area" if operation == "resize" else "Move area"
            self.edit_history.record_applied(
                ViewerEdit.geometry(editor_id, before, after, description=description)
            )
            self.parent.update_areas_treeview()

    def move_selected_rectangle_by_scene_delta(self, delta: QtCore.QPointF) -> None:
        item = self._selected_rect_item()
        if item is None:
            return
        rect = item.rect().translated(delta)
        self._apply_selected_scene_rect(rect)

    def resize_selected_rectangle_to_scene_pos(self, handle_name: str, pos: QtCore.QPointF) -> None:
        if handle_name not in self.RESIZE_HANDLE_NAMES:
            return
        item = self._selected_rect_item()
        if item is None:
            return

        rect = item.rect().normalized()
        left, top, right, bottom = rect.left(), rect.top(), rect.right(), rect.bottom()
        min_size = self.MIN_RECTANGLE_SCENE_SIZE

        if "left" in handle_name:
            left = min(pos.x(), right - min_size)
        if "right" in handle_name:
            right = max(pos.x(), left + min_size)
        if handle_name == "top":
            top = min(pos.y(), bottom - min_size)
        elif handle_name == "bottom":
            bottom = max(pos.y(), top + min_size)
        else:
            if "top" in handle_name:
                top = min(pos.y(), bottom - min_size)
            if "bottom" in handle_name:
                bottom = max(pos.y(), top + min_size)

        self._apply_selected_scene_rect(QtCore.QRectF(QtCore.QPointF(left, top), QtCore.QPointF(right, bottom)))

    def resize_handle_at_scene_pos(self, pos: QtCore.QPointF) -> str | None:
        for name, item in self.resize_handles.items():
            if item.sceneBoundingRect().adjusted(-2, -2, 2, 2).contains(pos):
                return name
        return None

    def cursor_for_resize_handle(self, handle_name: str) -> QtCore.Qt.CursorShape:
        return self._CORNER_CURSORS.get(handle_name, QtCore.Qt.ArrowCursor)

    def set_hovered_resize_handle(self, handle_name: str | None) -> None:
        if self._hovered_resize_handle == handle_name:
            return
        self._hovered_resize_handle = handle_name
        self._update_resize_handle_styles()

    def set_active_resize_handle(self, handle_name: str | None) -> None:
        if self._active_resize_handle == handle_name:
            return
        self._active_resize_handle = handle_name
        self._update_resize_handle_styles()

    def _selected_rect_item(self) -> QtWidgets.QGraphicsRectItem | None:
        index = self.selected_rectangle_index
        if index is not None and 0 <= index < len(self.rectangle_items):
            return self.rectangle_items[index]
        return None

    def _hover_rect_item(self) -> QtWidgets.QGraphicsRectItem | None:
        index = self.hover_rectangle_index
        if index is not None and 0 <= index < len(self.rectangle_items):
            return self.rectangle_items[index]
        return None

    def _resize_target_rect_item(self) -> QtWidgets.QGraphicsRectItem | None:
        if self.selection_mode != "resize":
            return None
        return self._hover_rect_item() or self._selected_rect_item()

    def _set_hover_rectangle(self, index: int | None) -> None:
        if self.hover_rectangle_index == index:
            return
        self.hover_rectangle_index = index
        self._hovered_resize_handle = None
        self._refresh_selection_visuals()

    def _apply_selected_scene_rect(self, rect: QtCore.QRectF) -> None:
        item = self._selected_rect_item()
        if item is None:
            return

        rect = rect.normalized()
        previous_rect = item.rect()
        item.setRect(rect)
        page_rect = [
                rect.left() / self.current_zoom,
                rect.top() / self.current_zoom,
                rect.right() / self.current_zoom,
                rect.bottom() / self.current_zoom,
            ]
        if not self._set_selected_model_rect(
            list(self._page_to_canonical_rect(page_rect))
        ):
            item.setRect(previous_rect)
        self._update_resize_handles()
        self._refresh_selection_visuals(update_handles=False)

    def _set_selected_model_rect(self, coordinates: list[float]) -> bool:
        if self.selected_rectangle_index is not None:
            if 0 <= self.selected_rectangle_index < len(self.areas):
                area = self.areas[self.selected_rectangle_index]
                try:
                    self.areas[self.selected_rectangle_index] = area.with_rect(
                        coordinates
                    )
                except AreaSpecValidationError as exc:
                    self._report_area_validation_error(exc)
                    return False
                if self._geometry_drag is None:
                    self.parent.update_areas_treeview()
                return True
        return False

    def _report_area_validation_error(self, error: AreaSpecValidationError) -> None:
        callback = getattr(self.parent, "set_area_edit_validation_error", None)
        if callable(callback):
            callback(str(error))

    def _clear_resize_handles(self) -> None:
        for item in self.resize_handles.values():
            self._safe_remove_item(item)
        self.resize_handles.clear()

    def _update_resize_handles(self) -> None:
        self._clear_resize_handles()
        if self.selection_mode != "resize":
            return
        item = self._resize_target_rect_item()
        if item is None:
            return

        rect = item.rect().normalized()
        cx = rect.center().x()
        cy = rect.center().y()
        points = {
            "top_left": QtCore.QPointF(rect.left(), rect.top()),
            "top": QtCore.QPointF(cx, rect.top()),
            "top_right": QtCore.QPointF(rect.right(), rect.top()),
            "right": QtCore.QPointF(rect.right(), cy),
            "bottom_right": QtCore.QPointF(rect.right(), rect.bottom()),
            "bottom": QtCore.QPointF(cx, rect.bottom()),
            "bottom_left": QtCore.QPointF(rect.left(), rect.bottom()),
            "left": QtCore.QPointF(rect.left(), cy),
        }
        size = self.HANDLE_SCENE_SIZE
        for name in self.RESIZE_HANDLE_NAMES:
            point = points[name]
            handle_rect = QtCore.QRectF(
                point.x() - (size / 2),
                point.y() - (size / 2),
                size,
                size,
            )
            handle = self.scene.addRect(handle_rect)
            handle.setData(0, name)
            handle.setZValue(3)
            self.resize_handles[name] = handle
        self._update_resize_handle_styles()

    def _update_resize_handle_styles(self) -> None:
        for name, item in self.resize_handles.items():
            if name == self._active_resize_handle:
                brush = QtGui.QBrush(QtGui.QColor("#ffca28"))
                pen = QtGui.QPen(QtGui.QColor("#ffffff"), 1.5)
            elif name == self._hovered_resize_handle:
                brush = QtGui.QBrush(QtGui.QColor("#ffffff"))
                pen = QtGui.QPen(QtGui.QColor("#ffca28"), 1.5)
            else:
                brush = QtGui.QBrush(QtGui.QColor("#1f1f1f"))
                pen = QtGui.QPen(QtGui.QColor("#ffca28"), 1.5)
            item.setBrush(brush)
            item.setPen(pen)

    def _refresh_selection_visuals(self, *, update_handles: bool = True) -> None:
        if self._selected_rect_item() is None:
            self.selected_rectangle_index = None
        if self._hover_rect_item() is None:
            self.hover_rectangle_index = None
        for index, rect_item in enumerate(self.rectangle_items):
            selected = self.selected_rectangle_index == index
            hovered = self.hover_rectangle_index == index
            rect_item.setPen(self._rectangle_pen(selected, hovered))

        if update_handles:
            self._update_resize_handles()

    def _rectangle_pen(self, selected: bool, hovered: bool = False) -> QtGui.QPen:
        if selected:
            pen = QtGui.QPen(QtGui.QColor("#ffca28"), 3)
            pen.setCosmetic(True)
            return pen
        if hovered:
            pen = QtGui.QPen(QtGui.QColor("#ffca28"), 2)
            pen.setStyle(QtCore.Qt.DashLine)
            pen.setCosmetic(True)
            return pen
        pen = QtGui.QPen(QtGui.QColor("red"), 2)
        pen.setCosmetic(True)
        return pen

    def set_zoom(self, zoom_level: float):
        self.current_zoom = zoom_level
        self.update_display()

    def show_context_menu(self, event: QtGui.QMouseEvent):
        pos = self.view.mapToScene(event.position().toPoint())
        hit = self.rectangle_at_scene_pos(pos, edge_tolerance=5)
        if hit is None:
            self.clear_rectangle_selection()
            return
        self.selected_rectangle_index = hit
        self._refresh_selection_visuals()

        menu = QtWidgets.QMenu(self)
        menu.addAction("Delete area", self.delete_selected_rectangle)
        menu.exec(event.globalPosition().toPoint())

    def delete_rectangle_at_scene_pos(self, pos: QtCore.QPointF) -> bool:
        hit = self.rectangle_at_scene_pos(pos)
        if hit is None:
            self.clear_rectangle_selection()
            self.view.refresh_interaction_cursor(pos)
            return False
        self.selected_rectangle_index = hit
        self._refresh_selection_visuals()
        self.delete_selected_rectangle()
        self.view.refresh_interaction_cursor(pos)
        return True

    def delete_selected_rectangle(self):
        if self.selected_rectangle_index is None:
            return
        index = self.selected_rectangle_index
        if 0 <= index < len(self.areas):
            if len(self.areas) == 1:
                self.edit_history.execute(
                    ViewerEdit.replace_state(
                        self._state_snapshot(),
                        ViewerStateSnapshot(),
                        description="Delete area",
                    )
                )
            else:
                self.edit_history.execute(
                    ViewerEdit.delete_area(
                        self._area_snapshot(
                            self.areas[index],
                            editor_id=self._area_editor_ids[index],
                        ),
                        index,
                    )
                )

    def highlight_rectangle(self, index: Optional[int]):
        if index is not None and 0 <= index < len(self.rectangle_items):
            self.selected_rectangle_index = index
        else:
            self.selected_rectangle_index = None
        self._refresh_selection_visuals()

    def get_gui_areas(self) -> list[AreaSpec]:
        """Return the viewer's canonical typed area state."""
        return list(self.areas)

    def area_editor_id(self, index: int) -> str:
        return self._area_editor_ids[index]

    def set_gui_areas(self, areas: Sequence[object]) -> None:
        normalized = self._normalize_area_batch(areas)
        self.edit_history.clear()
        self.areas = [area for area, _editor_id in normalized]
        self._area_editor_ids = [
            editor_id for _area, editor_id in normalized
        ]
        self.update_rectangles()
        self.view.refresh_interaction_cursor()

    def undo_edit(self) -> None:
        self.edit_history.undo()

    def redo_edit(self) -> None:
        self.edit_history.redo()

    def _handle_undo_shortcut(self) -> None:
        self.undo_edit()

    def _handle_redo_shortcut(self) -> None:
        self.redo_edit()

    def remove_all_areas(self) -> None:
        before = self._state_snapshot()
        if not before.areas:
            return
        self.edit_history.execute(
            ViewerEdit.replace_all(
                before,
                ViewerStateSnapshot(),
            )
        )

    def _new_editor_id(
        self,
        occupied_ids: Sequence[str] = (),
    ) -> str:
        occupied = set(self._area_editor_ids)
        occupied.update(occupied_ids)
        while True:
            editor_id = f"area-{self._next_editor_id}"
            self._next_editor_id += 1
            if editor_id not in occupied:
                return editor_id

    @staticmethod
    def _supplied_editor_id(area: object) -> str | None:
        if not isinstance(area, Mapping):
            return None
        raw_editor_id = area.get("_editor_id")
        return str(raw_editor_id) if raw_editor_id else None

    def _normalize_area_batch(
        self,
        areas: Sequence[object],
    ) -> list[tuple[AreaSpec, str]]:
        values = list(areas)
        normalized_specs = [
            normalize_area_spec(
                area,
                fallback_title=f"Area {index + 1}",
            )
            for index, area in enumerate(values)
        ]
        supplied_ids = [self._supplied_editor_id(area) for area in values]
        explicit_ids = [editor_id for editor_id in supplied_ids if editor_id]
        if len(explicit_ids) != len(set(explicit_ids)):
            raise ValueError("Area editor IDs must be unique.")

        occupied = set(self._area_editor_ids)
        occupied.update(explicit_ids)
        assigned_ids: list[str] = []
        for supplied_id in supplied_ids:
            editor_id = supplied_id
            if editor_id is None:
                editor_id = self._new_editor_id(occupied)
            assigned_ids.append(editor_id)
            occupied.add(editor_id)
        return list(zip(normalized_specs, assigned_ids))

    def _area_snapshot(
        self,
        area: AreaSpec,
        *,
        editor_id: str,
    ) -> AreaSnapshot:
        return AreaSnapshot.from_area_spec(area, editor_id=editor_id)

    def _state_snapshot(self) -> ViewerStateSnapshot:
        return ViewerStateSnapshot(
            areas=tuple(
                self._area_snapshot(area, editor_id=editor_id)
                for area, editor_id in zip(self.areas, self._area_editor_ids)
            ),
            coordinate_basis=self._coordinate_basis,
        )

    def _restore_state(self, state: ViewerStateSnapshot) -> None:
        self.areas = [snapshot.area for snapshot in state.areas]
        self._area_editor_ids = [snapshot.editor_id for snapshot in state.areas]
        self._coordinate_basis = state.coordinate_basis

    def _find_area_index(self, editor_id: str) -> int | None:
        for index, area_editor_id in enumerate(self._area_editor_ids):
            if area_editor_id == editor_id:
                return index
        return None

    def _selected_target(self) -> tuple[str, Coordinates] | None:
        index = self.selected_rectangle_index
        if index is not None and 0 <= index < len(self.areas):
            return self._area_editor_ids[index], self.areas[index].rect
        return None

    def _apply_viewer_edit(self, edit: ViewerEdit, forward: bool) -> None:
        if edit.kind == "add_area":
            snapshot = edit.after
            if not isinstance(snapshot, AreaSnapshot):
                return
            if forward:
                index = min(edit.index if edit.index is not None else len(self.areas), len(self.areas))
                self.areas.insert(index, snapshot.area)
                self._area_editor_ids.insert(index, snapshot.editor_id)
            else:
                index = self._find_area_index(snapshot.editor_id)
                if index is not None:
                    del self.areas[index]
                    del self._area_editor_ids[index]
        elif edit.kind == "delete_area":
            snapshot = edit.before
            if not isinstance(snapshot, AreaSnapshot):
                return
            if forward:
                index = self._find_area_index(snapshot.editor_id)
                if index is not None:
                    del self.areas[index]
                    del self._area_editor_ids[index]
            else:
                index = min(edit.index if edit.index is not None else len(self.areas), len(self.areas))
                self.areas.insert(index, snapshot.area)
                self._area_editor_ids.insert(index, snapshot.editor_id)
        elif edit.kind == "geometry":
            coordinates = edit.after if forward else edit.before
            if not isinstance(coordinates, tuple):
                return
            index = self._find_area_index(edit.target_id or "")
            if index is not None:
                self.areas[index] = self.areas[index].with_rect(coordinates)
        elif edit.kind == "replace_state":
            state = edit.after if forward else edit.before
            if not isinstance(state, ViewerStateSnapshot):
                return
            self._restore_state(state)

        self.clear_rectangle_selection()
        self.update_rectangles()
        self.view.refresh_interaction_cursor()

    def _safe_remove_item(self, item: QtWidgets.QGraphicsItem | None) -> None:
        if item is None:
            return
        try:
            scene = item.scene()
        except RuntimeError:
            return
        if scene is not None:
            scene.removeItem(item)

    def _reset_rectangle_state(self) -> None:
        self.rectangle_items.clear()
        self.current_rectangle = None
        self.selected_rectangle_index = None
        self.hover_rectangle_index = None
        self._hovered_resize_handle = None
        self._active_resize_handle = None
        self._clear_resize_handles()
