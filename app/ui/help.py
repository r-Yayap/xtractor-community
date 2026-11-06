from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from app.common.help_resources import (
    HELP_RESOURCES,
    get_help_resource,
    resolve_help_link,
    resolve_help_resource,
)
from app.logging_setup import log_file_path
from app.common.html_documents import read_html_file
from app.version import __version__

logger = logging.getLogger(__name__)

_HELP_UNAVAILABLE = "Help content is unavailable in this installation."
_LOGS_UNAVAILABLE = "The Xtractor logs folder is not available."
_LOGS_OPEN_FAILED = "The Xtractor logs folder could not be opened."


def open_local_path(path: Path) -> bool:
    """Ask the operating system to open one deliberate local file or folder."""

    return bool(QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path))))


class _OfflineHelpBrowser(QtWidgets.QTextBrowser):
    """Render supplied HTML without fetching external or embedded resources."""

    def loadResource(self, resource_type: int, name: QtCore.QUrl):  # noqa: N802
        if resource_type == QtGui.QTextDocument.ResourceType.ImageResource:
            return QtGui.QImage()
        return QtCore.QByteArray()


class HelpDocumentDialog(QtWidgets.QDialog):
    """A reusable, read-only native HTML viewer for offline Help content."""

    def __init__(
        self,
        parent: QtWidgets.QWidget,
        resource_id: str,
        markdown: str,
        document_path: Path,
        open_link,
        open_system=None,
    ) -> None:
        super().__init__(parent)
        self._open_link_callback = open_link
        self.setModal(False)
        self.resize(920, 720)

        layout = QtWidgets.QVBoxLayout(self)
        self.browser = _OfflineHelpBrowser(self)
        self.browser.setReadOnly(True)
        self.browser.setFont(QtGui.QFont("Segoe UI", 11))
        self.browser.setOpenLinks(False)
        self.browser.setOpenExternalLinks(False)
        self.browser.setStyleSheet("QTextBrowser { background: white; color: #1b2c42; padding: 14px; }")
        self.browser.document().setDefaultStyleSheet("body { font-family: 'Segoe UI'; font-size: 12pt; color: #1b2c42; } h1 { font-size: 23pt; } h2 { font-size: 18pt; margin-top: 24px; } p, li { line-height: 150%; } a { color: #175b91; } th, td { padding: 8px; }")
        self.browser.anchorClicked.connect(self._open_link)
        layout.addWidget(self.browser, 1)
        self.browser_button = QtWidgets.QPushButton("Open in browser", self)
        self.browser_button.setToolTip("Open this offline HTML document in your system browser")
        self.browser_button.setVisible(open_system is not None)
        if open_system is not None:
            self.browser_button.clicked.connect(lambda: open_system(self._resource_id))
        layout.addWidget(self.browser_button, 0, QtCore.Qt.AlignmentFlag.AlignLeft)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Close,
            parent=self,
        )
        buttons.rejected.connect(self.hide)
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Close).clicked.connect(
            self.hide
        )
        layout.addWidget(buttons)
        self.set_document(resource_id, markdown, document_path)

    def set_document(
        self,
        resource_id: str,
        markdown: str,
        document_path: Path,
    ) -> None:
        resource = get_help_resource(resource_id)
        self.setWindowTitle(f"{resource.title} — Xtractor Help")
        base_url = QtCore.QUrl.fromLocalFile(
            str(Path(document_path).resolve().parent) + "/"
        )
        self.browser.document().setBaseUrl(base_url)
        # Browser-only chrome is omitted in the native reader. The document
        # body, legal text, history notices and section anchors stay intact.
        main = re.search(r'<main\b[^>]*>(.*?)</main>', markdown, re.S | re.I)
        native = main.group(1) if main else markdown
        native = re.sub(r'<details class="toc"[^>]*>.*?</details>|<footer\b[^>]*>.*?</footer>', '', native, flags=re.S | re.I)
        self.browser.setHtml(native)
        self._resource_id = resource_id

    def _open_link(self, url: QtCore.QUrl) -> None:
        try:
            self._open_link_callback(self._resource_id, url)
        except Exception as exc:
            logger.warning(
                "Help link was unavailable (%s, %s)",
                self._resource_id,
                type(exc).__name__,
            )


class HelpSurface:
    """Native Help menu, F1 entry point, and safe local document viewer."""

    def __init__(self, parent: QtWidgets.QMainWindow, button: QtWidgets.QToolButton):
        self.parent = parent
        self.button = button
        self.fallback_viewer: HelpDocumentDialog | None = None

        self.menu = QtWidgets.QMenu(parent)
        self.menu.setObjectName("helpMenu")
        for resource in HELP_RESOURCES:
            if not resource.show_in_menu:
                continue
            action = self.menu.addAction(resource.title)
            action.setData(resource.resource_id)
            action.triggered.connect(
                lambda _checked=False, resource_id=resource.resource_id: self.open_document(
                    resource_id
                )
            )
        self.menu.addSeparator()
        self.menu.addAction("Open Logs Folder", self.open_logs_folder)
        self.menu.addAction("About Xtractor", self.show_about)

        self.button.clicked.connect(self.show_menu)
        self.shortcut = QtGui.QShortcut(QtGui.QKeySequence("F1"), parent)
        self.shortcut.setContext(QtCore.Qt.ShortcutContext.WindowShortcut)
        self.shortcut.activated.connect(self.show_menu)

    def show_menu(self) -> None:
        position = self.button.mapToGlobal(
            QtCore.QPoint(0, self.button.height())
        )
        self.menu.popup(position)

    def open_document(self, resource_id: str) -> None:
        if resource_id in {"software_license", "third_party_notices"}:
            self.open_document_in_fallback(resource_id)
            return
        try:
            get_help_resource(resource_id)
            path = resolve_help_resource(resource_id)
            if not path.is_file():
                raise OSError("Help resource is not a file.")
            markdown = read_html_file(path)
            if not markdown.strip():
                raise OSError("Help resource is empty.")
        except Exception as exc:
            logger.warning(
                "Help resource unavailable (%s, %s)",
                resource_id,
                type(exc).__name__,
            )
            self._show_warning("Help unavailable", _HELP_UNAVAILABLE)
            return

        try:
            if open_local_path(path):
                logger.info("Opened Help resource in system viewer (%s)", resource_id)
                return
        except Exception as exc:
            logger.warning(
                "Help system viewer failed (%s, %s)",
                resource_id,
                type(exc).__name__,
            )

        try:
            self._show_fallback_document(resource_id, markdown, path)
        except Exception as exc:
            logger.warning(
                "Help fallback viewer failed (%s, %s)",
                resource_id,
                type(exc).__name__,
            )
            self._show_warning("Help unavailable", _HELP_UNAVAILABLE)

    def _show_fallback_document(
        self,
        resource_id: str,
        markdown: str,
        path: Path,
    ) -> None:
        if self.fallback_viewer is None:
            self.fallback_viewer = HelpDocumentDialog(
                self.parent,
                resource_id,
                markdown,
                path,
                self._open_fallback_link,
                self.open_in_system_viewer,
            )
        else:
            self.fallback_viewer.set_document(resource_id, markdown, path)
        self.fallback_viewer.show()
        self.fallback_viewer.raise_()
        self.fallback_viewer.activateWindow()

    def _open_fallback_link(self, current_resource_id: str, url: QtCore.QUrl) -> None:
        if url.isLocalFile():
            target = self._resource_id_for_local_file(url)
            if target is not None:
                self.open_document_in_fallback(target, url.fragment())
            return
        resolved = resolve_help_link(current_resource_id, url.toString())
        if resolved is not None:
            self.open_document_in_fallback(resolved.resource.resource_id, resolved.fragment)

    @staticmethod
    def _resource_id_for_local_file(url: QtCore.QUrl) -> str | None:
        if not url.isLocalFile() or url.authority() or url.hasQuery():
            return None
        local_path = url.toLocalFile()
        if not local_path or local_path.startswith(("\\\\", "//")):
            return None

        candidate = os.path.normcase(os.path.abspath(os.path.normpath(local_path)))
        for resource in HELP_RESOURCES:
            try:
                allowlisted_path = resolve_help_resource(resource.resource_id)
            except Exception:
                continue
            expected = os.path.normcase(
                os.path.abspath(os.path.normpath(str(allowlisted_path)))
            )
            if candidate == expected:
                return resource.resource_id
        return None

    def open_document_in_fallback(self, resource_id: str, fragment: str = "") -> None:
        try:
            get_help_resource(resource_id)
            path = resolve_help_resource(resource_id)
            if not path.is_file():
                raise OSError("Help resource is not a file.")
            markdown = read_html_file(path)
            if not markdown.strip():
                raise OSError("Help resource is empty.")
            self._show_fallback_document(resource_id, markdown, path)
            if fragment and self.fallback_viewer is not None:
                self.fallback_viewer.browser.scrollToAnchor(fragment)
        except Exception as exc:
            logger.warning(
                "Help link target unavailable (%s, %s)",
                resource_id,
                type(exc).__name__,
            )
            self._show_warning("Help unavailable", _HELP_UNAVAILABLE)

    def open_in_system_viewer(self, resource_id: str) -> None:
        """An explicit user action opens only the validated local HTML reader."""
        try:
            path = resolve_help_resource(resource_id)
            read_html_file(path)
            if open_local_path(path):
                return
        except Exception as exc:
            logger.warning("Help browser unavailable (%s)", type(exc).__name__)
        self._show_warning("Help unavailable", _HELP_UNAVAILABLE)

    def open_logs_folder(self) -> None:
        try:
            logs_dir = Path(log_file_path()).parent
            if not logs_dir.is_dir():
                logger.info("Configured logs folder is unavailable.")
                self._show_warning("Logs unavailable", _LOGS_UNAVAILABLE)
                return
        except Exception as exc:
            logger.warning("Logs folder resolution failed (%s)", type(exc).__name__)
            self._show_warning("Logs unavailable", _LOGS_UNAVAILABLE)
            return

        try:
            if open_local_path(logs_dir):
                return
        except Exception as exc:
            logger.warning("Logs folder open failed (%s)", type(exc).__name__)
        self._show_warning("Logs unavailable", _LOGS_OPEN_FAILED)

    def show_about(self) -> None:
        QtWidgets.QMessageBox.about(
            self.parent,
            "About Xtractor",
            f"Xtractor\nVersion {__version__}",
        )

    def _show_warning(self, title: str, message: str) -> None:
        QtWidgets.QMessageBox.warning(self.parent, title, message)
