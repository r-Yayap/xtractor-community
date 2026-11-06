from __future__ import annotations

import sys
import time
from collections.abc import Mapping

from PySide6 import QtCore, QtGui, QtSvg, QtWidgets


def _load_svg_icon(path: str, size: QtCore.QSize) -> QtGui.QIcon:
    """Load an SVG icon even when the active Qt platform lacks an icon engine."""
    icon = QtGui.QIcon(path)
    if not icon.isNull():
        return icon

    renderer = QtSvg.QSvgRenderer(path)
    if not renderer.isValid():
        return QtGui.QIcon()

    pixmap = QtGui.QPixmap(size)
    pixmap.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    return QtGui.QIcon(pixmap)


class WindowControlButton(QtWidgets.QToolButton):
    """Title-bar control button with QSS-addressable pointer state."""

    def __init__(self, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_Hover, True)
        self.setMouseTracking(True)
        self._set_title_state("")

    def event(self, event: QtCore.QEvent) -> bool:
        event_type = event.type()
        if event_type == QtCore.QEvent.Type.HoverEnter:
            self._set_title_state("hover")
        elif event_type == QtCore.QEvent.Type.HoverLeave:
            self._set_title_state("")
        elif event_type == QtCore.QEvent.Type.HoverMove and self.underMouse():
            self._set_title_state("hover")
        return super().event(event)

    def enterEvent(self, event: QtCore.QEvent) -> None:
        self._set_title_state("hover")
        super().enterEvent(event)

    def leaveEvent(self, event: QtCore.QEvent) -> None:
        self._set_title_state("")
        super().leaveEvent(event)

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self._set_title_state("pressed")
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        super().mouseReleaseEvent(event)
        self._set_title_state("hover" if self.underMouse() else "")

    def _set_title_state(self, state: str) -> None:
        if self.property("titleState") == state:
            return
        self.setProperty("titleState", state)
        style = self.style()
        if style is not None:
            style.unpolish(self)
            style.polish(self)
        self.update()


class FramelessMainWindow(QtWidgets.QMainWindow):
    """Reusable main window shell with Xtractor's custom chrome."""


    def __init__(
        self,
        *,
        visible_title: str,
        logo_path: str = "",
        control_icon_paths: Mapping[str, str] | None = None,
        parent: QtWidgets.QWidget | None = None,
    ):
        super().__init__(parent)
        self._window_body_widget: QtWidgets.QWidget | None = None
        self._control_icon_paths = dict(control_icon_paths or {})
        self._chrome_frame: QtWidgets.QFrame | None = None
        self._header_container: QtWidgets.QWidget | None = None
        self._body_container: QtWidgets.QWidget | None = None
        self._drag_handle: QtWidgets.QWidget | None = None
        self._resize_event_widgets: list[QtWidgets.QWidget] = []
        self._resize_handles: dict[
            QtWidgets.QWidget,
            QtCore.Qt.Edge,
        ] = {}
        self._resize_margin = 8
        self._window_corner_radius = 14
        self._is_dragging_window = False
        self._drag_offset = QtCore.QPoint()
        self._is_resizing_window = False
        self._resize_edges = QtCore.Qt.Edge(0)
        self._resize_start_geometry = QtCore.QRect()
        self._resize_start_global = QtCore.QPoint()
        self._windows_snap_style_configured = False
        self._last_max_restore_request_at = 0.0
        self._last_max_restore_request_source = ""

        self.setWindowFlags(
            self.windowFlags()
            | QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.WindowSystemMenuHint
            | QtCore.Qt.WindowType.WindowMinimizeButtonHint
            | QtCore.Qt.WindowType.WindowMaximizeButtonHint
            | QtCore.Qt.WindowType.WindowCloseButtonHint
        )
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(
            QtCore.Qt.WidgetAttribute.WA_TranslucentBackground,
            True,
        )
        self.setMouseTracking(True)
        self._build_window_chrome(visible_title, logo_path)

    @property
    def window_body_widget(self) -> QtWidgets.QWidget | None:
        return self._window_body_widget

    def set_visible_title(self, title: str) -> None:
        self._title_label.setText(title)

    def set_window_body(self, widget: QtWidgets.QWidget) -> None:
        if self._window_body_widget is widget:
            return
        if self._window_body_widget is not None:
            self._body_layout.removeWidget(self._window_body_widget)
            self._window_body_widget.setParent(None)
        self._window_body_widget = widget
        self._body_layout.addWidget(widget)

    def _build_window_chrome(self, visible_title: str, logo_path: str) -> None:
        central = QtWidgets.QWidget(self)
        outer_layout = QtWidgets.QVBoxLayout(central)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        chrome = QtWidgets.QFrame(central)
        chrome.setObjectName("WindowChrome")
        chrome.setMouseTracking(True)
        chrome.installEventFilter(self)
        chrome_layout = QtWidgets.QVBoxLayout(chrome)
        chrome_layout.setContentsMargins(0, 0, 0, 0)
        chrome_layout.setSpacing(0)
        outer_layout.addWidget(chrome)
        self._chrome_frame = chrome

        header = QtWidgets.QWidget(chrome)
        header.setObjectName("WindowHeader")
        header.setFixedHeight(34)
        header.setMouseTracking(True)
        header.installEventFilter(self)
        header_layout = QtWidgets.QHBoxLayout(header)
        header_layout.setContentsMargins(16, 0, 0, 0)
        header_layout.setSpacing(8)
        self._header_container = header

        logo_label = QtWidgets.QLabel(header)
        logo_label.setObjectName("WindowLogo")
        logo_label.setFixedSize(20, 20)
        logo_label.setScaledContents(True)
        logo_icon = _load_svg_icon(logo_path, QtCore.QSize(20, 20))
        if not logo_icon.isNull():
            logo_label.setPixmap(logo_icon.pixmap(QtCore.QSize(20, 20)))
        header_layout.addWidget(logo_label)
        self._logo_label = logo_label

        title_label = QtWidgets.QLabel(visible_title, header)
        title_label.setObjectName("WindowTitle")
        title_label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        header_layout.addWidget(title_label)
        self._title_label = title_label

        drag_area = QtWidgets.QWidget(header)
        drag_area.setObjectName("WindowDragArea")
        drag_area.setCursor(QtCore.Qt.CursorShape.OpenHandCursor)
        drag_area.setMouseTracking(True)
        drag_area.installEventFilter(self)
        drag_area.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Preferred,
        )
        header_layout.addWidget(drag_area, 1)
        self._drag_handle = drag_area

        controls = QtWidgets.QWidget(header)
        controls.setObjectName("WindowControls")
        controls.setFixedHeight(34)
        controls_layout = QtWidgets.QHBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(10)

        self.btn_help = self._create_control_button(
            controls,
            "TitleHelpButton",
            "help",
            "Help (F1)",
        )
        self.btn_help.setText("?")
        self.btn_help.setAccessibleName("Help")
        self.btn_help.setAccessibleDescription("Open offline Xtractor Help.")
        controls_layout.addWidget(self.btn_help)
        self.btn_minimize = self._create_control_button(
            controls,
            "TitleMinButton",
            "minimize",
            "Minimize",
        )
        self.btn_maximize = self._create_control_button(
            controls,
            "TitleMaxButton",
            "maximize",
            "Maximize",
        )
        self.btn_close = self._create_control_button(
            controls,
            "TitleCloseButton",
            "close",
            "Close",
        )
        for button in (
            self.btn_minimize,
            self.btn_maximize,
            self.btn_close,
        ):
            controls_layout.addWidget(button)
        header_layout.addWidget(
            controls,
            0,
            QtCore.Qt.AlignmentFlag.AlignRight,
        )

        self.btn_minimize.clicked.connect(self.showMinimized)
        self.btn_maximize.clicked.connect(self._request_max_restore)
        self.btn_close.clicked.connect(self.close)

        chrome_layout.addWidget(header)

        body = QtWidgets.QWidget(chrome)
        body.setObjectName("WindowBody")
        body.setMouseTracking(True)
        body.installEventFilter(self)
        body_layout = QtWidgets.QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)
        chrome_layout.addWidget(body, 1)
        self._body_container = body
        self._body_layout = body_layout

        self._build_resize_handles(chrome)
        super().setCentralWidget(central)

    def _build_resize_handles(self, chrome: QtWidgets.QWidget) -> None:
        handles = (
            ("Left", QtCore.Qt.Edge.LeftEdge),
            ("Right", QtCore.Qt.Edge.RightEdge),
            ("Top", QtCore.Qt.Edge.TopEdge),
            ("Bottom", QtCore.Qt.Edge.BottomEdge),
            (
                "TopLeft",
                QtCore.Qt.Edge.LeftEdge | QtCore.Qt.Edge.TopEdge,
            ),
            (
                "TopRight",
                QtCore.Qt.Edge.RightEdge | QtCore.Qt.Edge.TopEdge,
            ),
            (
                "BottomLeft",
                QtCore.Qt.Edge.LeftEdge | QtCore.Qt.Edge.BottomEdge,
            ),
            (
                "BottomRight",
                QtCore.Qt.Edge.RightEdge | QtCore.Qt.Edge.BottomEdge,
            ),
        )
        for name, edges in handles:
            handle = QtWidgets.QWidget(chrome)
            handle.setObjectName("WindowResizeHandle")
            handle.setProperty("resizePosition", name)
            handle.setMouseTracking(True)
            handle.installEventFilter(self)
            cursor = self._cursor_for_edges(edges)
            if cursor is not None:
                handle.setCursor(cursor)
            self._resize_handles[handle] = edges
        self._resize_event_widgets = list(self._resize_handles)
        self._position_resize_handles()

    def _position_resize_handles(self) -> None:
        if self._chrome_frame is None:
            return
        width = self._chrome_frame.width()
        height = self._chrome_frame.height()
        margin = self._resize_margin
        corner = margin
        geometries = {
            QtCore.Qt.Edge.LeftEdge: QtCore.QRect(
                0,
                corner,
                margin,
                max(0, height - corner * 2),
            ),
            QtCore.Qt.Edge.RightEdge: QtCore.QRect(
                max(0, width - margin),
                corner,
                margin,
                max(0, height - corner * 2),
            ),
            QtCore.Qt.Edge.TopEdge: QtCore.QRect(
                corner,
                0,
                max(0, width - corner * 2),
                margin,
            ),
            QtCore.Qt.Edge.BottomEdge: QtCore.QRect(
                corner,
                max(0, height - margin),
                max(0, width - corner * 2),
                margin,
            ),
            QtCore.Qt.Edge.LeftEdge
            | QtCore.Qt.Edge.TopEdge: QtCore.QRect(
                0,
                0,
                corner,
                corner,
            ),
            QtCore.Qt.Edge.RightEdge
            | QtCore.Qt.Edge.TopEdge: QtCore.QRect(
                max(0, width - corner),
                0,
                corner,
                corner,
            ),
            QtCore.Qt.Edge.LeftEdge
            | QtCore.Qt.Edge.BottomEdge: QtCore.QRect(
                0,
                max(0, height - corner),
                corner,
                corner,
            ),
            QtCore.Qt.Edge.RightEdge
            | QtCore.Qt.Edge.BottomEdge: QtCore.QRect(
                max(0, width - corner),
                max(0, height - corner),
                corner,
                corner,
            ),
        }
        enabled = not self.isMaximized() and not self.isFullScreen()
        for handle, edges in self._resize_handles.items():
            handle.setGeometry(geometries[edges])
            handle.setVisible(enabled)
            if enabled:
                handle.raise_()

    def _create_control_button(
        self,
        parent: QtWidgets.QWidget,
        object_name: str,
        icon_key: str,
        tooltip: str,
    ) -> WindowControlButton:
        button = WindowControlButton(parent)
        button.setObjectName(object_name)
        button.setText("")
        button.setIconSize(QtCore.QSize(16, 16))
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip)
        button.setAutoRaise(True)
        button.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        button.setFixedSize(34, 34)
        icon = _load_svg_icon(
            self._control_icon_paths.get(icon_key, ""),
            QtCore.QSize(16, 16),
        )
        if not icon.isNull():
            button.setIcon(icon)
        return button

    def _toggle_max_restore(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()
        self._sync_window_buttons()

    def _request_max_restore(self, source: object = "qt") -> None:
        source_name = "qt" if isinstance(source, bool) else str(source or "qt")
        now = time.monotonic()
        if (
            source_name != self._last_max_restore_request_source
            and now - self._last_max_restore_request_at < 0.25
        ):
            return
        self._last_max_restore_request_at = now
        self._last_max_restore_request_source = source_name
        self._toggle_max_restore()

    def _sync_window_buttons(self) -> None:
        self.btn_maximize.setToolTip(
            "Restore Down" if self.isMaximized() else "Maximize"
        )
        self._sync_chrome_state_style()

    def _sync_chrome_state_style(self) -> None:
        state = (
            "maximized"
            if self.isMaximized() or self.isFullScreen()
            else ""
        )
        for widget in (
            self._chrome_frame,
            self._header_container,
            self._body_container,
        ):
            if widget is None or widget.property("windowState") == state:
                continue
            widget.setProperty("windowState", state)
            style = widget.style()
            if style is not None:
                style.unpolish(widget)
                style.polish(widget)
            widget.update()

    @staticmethod
    def _calculate_resized_geometry(
        start: QtCore.QRect,
        delta: QtCore.QPoint,
        edges: QtCore.Qt.Edge,
        minimum: QtCore.QSize,
        maximum: QtCore.QSize,
    ) -> QtCore.QRect:
        left, top = start.left(), start.top()
        right = start.left() + start.width()
        bottom = start.top() + start.height()

        if edges & QtCore.Qt.Edge.LeftEdge:
            left = min(left + delta.x(), right - minimum.width())
            if maximum.width() < 16777215:
                left = max(left, right - maximum.width())
        if edges & QtCore.Qt.Edge.RightEdge:
            right = max(right + delta.x(), left + minimum.width())
            if maximum.width() < 16777215:
                right = min(right, left + maximum.width())
        if edges & QtCore.Qt.Edge.TopEdge:
            top = min(top + delta.y(), bottom - minimum.height())
            if maximum.height() < 16777215:
                top = max(top, bottom - maximum.height())
        if edges & QtCore.Qt.Edge.BottomEdge:
            bottom = max(bottom + delta.y(), top + minimum.height())
            if maximum.height() < 16777215:
                bottom = min(bottom, top + maximum.height())

        return QtCore.QRect(left, top, right - left, bottom - top)

    def _detect_resize_edges_at(
        self,
        global_point: QtCore.QPoint,
    ) -> QtCore.Qt.Edge:
        if (
            self._chrome_frame is None
            or self.isMaximized()
            or self.isFullScreen()
        ):
            return QtCore.Qt.Edge(0)
        pos = self._chrome_frame.mapFromGlobal(global_point)
        rect = self._chrome_frame.rect()
        margin = self._resize_margin
        edges = QtCore.Qt.Edge(0)
        if pos.x() <= margin:
            edges |= QtCore.Qt.Edge.LeftEdge
        elif pos.x() >= rect.width() - margin:
            edges |= QtCore.Qt.Edge.RightEdge
        if pos.y() <= margin:
            edges |= QtCore.Qt.Edge.TopEdge
        elif pos.y() >= rect.height() - margin:
            edges |= QtCore.Qt.Edge.BottomEdge
        return edges

    @staticmethod
    def _cursor_for_edges(
        edges: QtCore.Qt.Edge,
    ) -> QtCore.Qt.CursorShape | None:
        if not edges:
            return None
        if (
            (
                edges & QtCore.Qt.Edge.LeftEdge
                and edges & QtCore.Qt.Edge.TopEdge
            )
            or (
                edges & QtCore.Qt.Edge.RightEdge
                and edges & QtCore.Qt.Edge.BottomEdge
            )
        ):
            return QtCore.Qt.CursorShape.SizeFDiagCursor
        if (
            (
                edges & QtCore.Qt.Edge.RightEdge
                and edges & QtCore.Qt.Edge.TopEdge
            )
            or (
                edges & QtCore.Qt.Edge.LeftEdge
                and edges & QtCore.Qt.Edge.BottomEdge
            )
        ):
            return QtCore.Qt.CursorShape.SizeBDiagCursor
        if (
            edges & QtCore.Qt.Edge.LeftEdge
            or edges & QtCore.Qt.Edge.RightEdge
        ):
            return QtCore.Qt.CursorShape.SizeHorCursor
        if (
            edges & QtCore.Qt.Edge.TopEdge
            or edges & QtCore.Qt.Edge.BottomEdge
        ):
            return QtCore.Qt.CursorShape.SizeVerCursor
        return None

    @staticmethod
    def _native_resize_hit_code(edges: QtCore.Qt.Edge) -> int | None:
        if not edges:
            return None
        if (
            edges & QtCore.Qt.Edge.LeftEdge
            and edges & QtCore.Qt.Edge.TopEdge
        ):
            return 13
        if (
            edges & QtCore.Qt.Edge.RightEdge
            and edges & QtCore.Qt.Edge.TopEdge
        ):
            return 14
        if (
            edges & QtCore.Qt.Edge.LeftEdge
            and edges & QtCore.Qt.Edge.BottomEdge
        ):
            return 16
        if (
            edges & QtCore.Qt.Edge.RightEdge
            and edges & QtCore.Qt.Edge.BottomEdge
        ):
            return 17
        if edges & QtCore.Qt.Edge.LeftEdge:
            return 10
        if edges & QtCore.Qt.Edge.RightEdge:
            return 11
        if edges & QtCore.Qt.Edge.TopEdge:
            return 12
        if edges & QtCore.Qt.Edge.BottomEdge:
            return 15
        return None

    def _try_start_system_move(self) -> bool:
        window = self.windowHandle()
        if window is None or not hasattr(window, "startSystemMove"):
            return False
        try:
            return bool(window.startSystemMove())
        except (RuntimeError, TypeError):
            return False

    def _try_start_system_resize(self, edges: QtCore.Qt.Edge) -> bool:
        window = self.windowHandle()
        if window is None or not hasattr(window, "startSystemResize"):
            return False
        resize_edges = QtCore.Qt.Edge(0)
        for edge in (
            QtCore.Qt.Edge.LeftEdge,
            QtCore.Qt.Edge.RightEdge,
            QtCore.Qt.Edge.TopEdge,
            QtCore.Qt.Edge.BottomEdge,
        ):
            if edges & edge:
                resize_edges |= edge
        try:
            return bool(window.startSystemResize(resize_edges))
        except (RuntimeError, TypeError):
            return False

    @staticmethod
    def _global_point_from_event(
        event: QtGui.QMouseEvent,
    ) -> QtCore.QPoint:
        if hasattr(event, "globalPosition"):
            return event.globalPosition().toPoint()
        return event.globalPos()

    def _start_window_drag(self, event: QtGui.QMouseEvent) -> None:
        self._is_dragging_window = True
        if self._drag_handle is not None:
            self._drag_handle.setCursor(
                QtCore.Qt.CursorShape.ClosedHandCursor
            )
        self._drag_offset = (
            self._global_point_from_event(event)
            - self.frameGeometry().topLeft()
        )

    def _perform_window_drag(self, event: QtGui.QMouseEvent) -> None:
        if (
            self._is_dragging_window
            and not self.isMaximized()
            and not self.isFullScreen()
        ):
            self.move(
                self._global_point_from_event(event) - self._drag_offset
            )

    def _finish_window_drag(self) -> None:
        self._is_dragging_window = False
        if self._drag_handle is not None:
            self._drag_handle.setCursor(
                QtCore.Qt.CursorShape.OpenHandCursor
            )

    def _start_window_resize(
        self,
        event: QtGui.QMouseEvent,
        edges: QtCore.Qt.Edge,
    ) -> None:
        if self.isMaximized() or self.isFullScreen():
            return
        self._is_resizing_window = True
        self._resize_edges = edges
        self._resize_start_geometry = self.geometry()
        self._resize_start_global = self._global_point_from_event(event)
        self._apply_resize_cursor(edges)

    def _perform_window_resize(self, event: QtGui.QMouseEvent) -> None:
        if not self._is_resizing_window:
            return
        current_global = self._global_point_from_event(event)
        geometry = self._calculate_resized_geometry(
            self._resize_start_geometry,
            current_global - self._resize_start_global,
            self._resize_edges,
            self.minimumSize(),
            self.maximumSize(),
        )
        self.setGeometry(geometry)

    def _finish_window_resize(self) -> None:
        self._is_resizing_window = False
        self._resize_edges = QtCore.Qt.Edge(0)
        self._unset_resize_cursor()

    def _update_resize_cursor_at(
        self,
        global_point: QtCore.QPoint,
    ) -> None:
        if (
            self._is_dragging_window
            or self._chrome_frame is None
            or self.isMaximized()
            or self.isFullScreen()
        ):
            return
        edges = self._detect_resize_edges_at(global_point)
        if edges:
            self._apply_resize_cursor(edges)
        elif not self._is_resizing_window:
            self._unset_resize_cursor()

    def _apply_resize_cursor(self, edges: QtCore.Qt.Edge) -> None:
        cursor = self._cursor_for_edges(edges)
        if cursor is None:
            return
        self.setCursor(cursor)

    def _unset_resize_cursor(self) -> None:
        self.unsetCursor()
        for handle, edges in self._resize_handles.items():
            cursor = self._cursor_for_edges(edges)
            if cursor is None:
                handle.unsetCursor()
            else:
                handle.setCursor(cursor)
        if self._drag_handle is not None and not self._is_dragging_window:
            self._drag_handle.setCursor(
                QtCore.Qt.CursorShape.OpenHandCursor
            )

    def eventFilter(
        self,
        watched: QtCore.QObject,
        event: QtCore.QEvent,
    ) -> bool:
        if (
            watched is self._chrome_frame
            and event.type() == QtCore.QEvent.Type.Resize
        ):
            self._position_resize_handles()

        if watched is self._drag_handle and self._drag_handle is not None:
            if event.type() == QtCore.QEvent.Type.MouseButtonPress:
                if (
                    isinstance(event, QtGui.QMouseEvent)
                    and event.button() == QtCore.Qt.MouseButton.LeftButton
                ):
                    if self._try_start_system_move():
                        return True
                    self._start_window_drag(event)
                    return True
            elif (
                event.type() == QtCore.QEvent.Type.MouseMove
                and self._is_dragging_window
                and isinstance(event, QtGui.QMouseEvent)
            ):
                self._perform_window_drag(event)
                return True
            elif event.type() == QtCore.QEvent.Type.MouseButtonRelease:
                if (
                    isinstance(event, QtGui.QMouseEvent)
                    and event.button() == QtCore.Qt.MouseButton.LeftButton
                ):
                    self._finish_window_drag()
                    return True
            elif (
                event.type() == QtCore.QEvent.Type.MouseButtonDblClick
                and isinstance(event, QtGui.QMouseEvent)
                and event.button() == QtCore.Qt.MouseButton.LeftButton
            ):
                self._toggle_max_restore()
                return True

        if watched in self._resize_handles:
            handle_edges = self._resize_handles[watched]
            if (
                event.type() == QtCore.QEvent.Type.MouseMove
                and isinstance(event, QtGui.QMouseEvent)
            ):
                if self._is_resizing_window:
                    self._perform_window_resize(event)
                    return True
            elif event.type() == QtCore.QEvent.Type.MouseButtonPress:
                if (
                    isinstance(event, QtGui.QMouseEvent)
                    and event.button() == QtCore.Qt.MouseButton.LeftButton
                ):
                    if (
                        handle_edges
                        and not self._is_dragging_window
                        and not self.isMaximized()
                        and not self.isFullScreen()
                    ):
                        if self._try_start_system_resize(handle_edges):
                            return True
                        self._start_window_resize(event, handle_edges)
                        return True
            elif event.type() == QtCore.QEvent.Type.MouseButtonRelease:
                if (
                    isinstance(event, QtGui.QMouseEvent)
                    and event.button() == QtCore.Qt.MouseButton.LeftButton
                    and self._is_resizing_window
                ):
                    self._finish_window_resize()
                    return True
            elif (
                event.type() == QtCore.QEvent.Type.Leave
                and not self._is_resizing_window
            ):
                self._unset_resize_cursor()

        return super().eventFilter(watched, event)

    def _apply_window_corner_mask(self) -> None:
        if (
            QtGui.QGuiApplication.platformName() == "offscreen"
            or self.isMaximized()
            or self.isFullScreen()
        ):
            self.clearMask()
            return
        path = QtGui.QPainterPath()
        path.addRoundedRect(
            QtCore.QRectF(self.rect()),
            self._window_corner_radius,
            self._window_corner_radius,
        )
        self.setMask(QtGui.QRegion(path.toFillPolygon().toPolygon()))

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        super().showEvent(event)
        self._configure_windows_snap_styles()
        self._sync_chrome_state_style()
        self._apply_window_corner_mask()

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        self._position_resize_handles()
        self._apply_window_corner_mask()

    def changeEvent(self, event: QtCore.QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QtCore.QEvent.Type.WindowStateChange:
            self._sync_window_buttons()
            self._position_resize_handles()
            self._apply_window_corner_mask()
            self._unset_resize_cursor()

    def _configure_windows_snap_styles(self) -> None:
        if (
            sys.platform != "win32"
            or QtGui.QGuiApplication.platformName() == "offscreen"
            or self._windows_snap_style_configured
        ):
            return

        import ctypes

        hwnd = int(self.winId())
        if not hwnd:
            return

        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            pointer_type = (
                ctypes.c_longlong
                if ctypes.sizeof(ctypes.c_void_p) == 8
                else ctypes.c_long
            )
            configured = self._apply_windows_snap_style(
                user32,
                hwnd,
                pointer_type,
            )
        except (AttributeError, OSError, ValueError):
            return

        self._windows_snap_style_configured = configured

    @staticmethod
    def _apply_windows_snap_style(
        user32,
        hwnd: int,
        pointer_type,
    ) -> bool:
        import ctypes

        get_window_long = getattr(
            user32,
            "GetWindowLongPtrW",
            user32.GetWindowLongW,
        )
        set_window_long = getattr(
            user32,
            "SetWindowLongPtrW",
            user32.SetWindowLongW,
        )
        get_window_long.argtypes = (ctypes.c_void_p, ctypes.c_int)
        get_window_long.restype = pointer_type
        set_window_long.argtypes = (
            ctypes.c_void_p,
            ctypes.c_int,
            pointer_type,
        )
        set_window_long.restype = pointer_type
        user32.SetWindowPos.argtypes = (
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
        )
        user32.SetWindowPos.restype = ctypes.c_bool

        ctypes.set_last_error(0)
        current_style = int(get_window_long(hwnd, -16))
        if current_style == 0 and ctypes.get_last_error() != 0:
            return False

        snap_style = (
            0x00C00000
            | 0x00040000
            | 0x00080000
            | 0x00020000
            | 0x00010000
        )
        updated_style = current_style | snap_style
        if updated_style == current_style:
            return True

        ctypes.set_last_error(0)
        previous_style = int(
            set_window_long(
                hwnd,
                -16,
                pointer_type(updated_style),
            )
        )
        if previous_style == 0 and ctypes.get_last_error() != 0:
            return False

        return bool(
            user32.SetWindowPos(
                hwnd,
                0,
                0,
                0,
                0,
                0,
                0x0001
                | 0x0002
                | 0x0004
                | 0x0010
                | 0x0020
                | 0x0200,
            )
        )

    @staticmethod
    def _global_point_in_widget(
        global_point: QtCore.QPoint,
        widget: QtWidgets.QWidget | None,
    ) -> bool:
        return bool(
            widget is not None
            and widget.isVisible()
            and widget.isEnabled()
            and widget.rect().contains(widget.mapFromGlobal(global_point))
        )

    def nativeEvent(self, event_type, message):
        if sys.platform != "win32":
            return super().nativeEvent(event_type, message)

        from ctypes.wintypes import MSG

        try:
            message_address = message.__int__()
        except AttributeError:
            message_address = int(message)
        msg = MSG.from_address(message_address)

        if msg.message == 0x0083 and msg.wParam:
            return True, 0

        global_point = QtGui.QCursor.pos()
        if msg.message == 0x00A2 and self._global_point_in_widget(
            global_point,
            self.btn_maximize,
        ):
            self._request_max_restore("native")
            return True, 0

        if msg.message != 0x0084:
            return super().nativeEvent(event_type, message)

        if self._global_point_in_widget(
            global_point,
            self.btn_maximize,
        ):
            return True, 9

        hit_code = self._native_resize_hit_code(
            self._detect_resize_edges_at(global_point)
        )
        if hit_code is not None:
            return True, hit_code

        if self._global_point_in_widget(
            global_point,
            self._drag_handle,
        ):
            return True, 2

        return super().nativeEvent(event_type, message)
