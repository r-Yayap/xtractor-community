from __future__ import annotations

import os
from collections import deque
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from app.services.pdf_selection_service import (
    PdfScanBranch,
    PdfScanManifest,
    PdfScanResult,
    PdfTreeNode,
)
from app.ui.pdf_scan_status import PdfScanStatusPanel
from app.ui.pdf_selection import PdfBranchWorker, PdfManifestWorker, PdfScanWorker
from app.ui.theme import Theme
from app.ui.widgets import DropFolderCard, FilesDropZone
from app.workflows.pdf_selection import PdfSelectionCoordinator


class PdfSelectionPanel(QtWidgets.QWidget):
    """Files presentation and scan lifetime for an injected selection owner."""

    folder_browse_requested = QtCore.Signal()
    paths_dropped = QtCore.Signal(object)
    selection_changed = QtCore.Signal()
    state_changed = QtCore.Signal()
    scan_finished = QtCore.Signal()
    preview_requested = QtCore.Signal(str)

    PDF_PATH_ROLE = int(QtCore.Qt.UserRole)
    PDF_TOTAL_ROLE = PDF_PATH_ROLE + 1
    PDF_SELECTED_ROLE = PDF_PATH_ROLE + 2
    PDF_BRANCH_ROLE = PDF_PATH_ROLE + 3
    PDF_BRANCH_STATE_ROLE = PDF_PATH_ROLE + 4
    PDF_TREE_TIME_BUDGET_MS = 6

    def __init__(
        self,
        *,
        selection: PdfSelectionCoordinator,
        theme: Theme,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.selection = selection
        self.theme = theme
        self._shutdown = False
        # Key every worker by request: replacing a scan must not release a
        # still-running worker or let its terminal callback remove the new one.
        self._pdf_scan_workers: dict[int, PdfScanWorker] = {}
        self._pdf_manifest_workers: dict[int, PdfManifestWorker] = {}
        self._pdf_branch_workers: dict[tuple[int, str], PdfBranchWorker] = {}
        self._pdf_branch_items: dict[str, QtWidgets.QTreeWidgetItem] = {}
        self._pdf_group_items: dict[tuple[str, ...], QtWidgets.QTreeWidgetItem] = {}
        self._pdf_tree_queue: deque[
            tuple[QtWidgets.QTreeWidgetItem | None, PdfTreeNode, int]
        ] = deque()
        self._pdf_tree_build_request_id = 0
        self._pdf_tree_max_width = 0
        self._pdf_thread_pool = QtCore.QThreadPool(self)
        self._pdf_thread_pool.setMaxThreadCount(4)
        self._pdf_stall_timer = QtCore.QTimer(self)
        self._pdf_stall_timer.setInterval(1000)
        self._pdf_stall_timer.timeout.connect(self._update_pdf_stalled_states)
        self._pdf_tree_timer = QtCore.QTimer(self)
        self._pdf_tree_timer.setSingleShot(True)
        self._pdf_tree_timer.timeout.connect(
            lambda: self._continue_pdf_tree_build(self._pdf_tree_build_request_id)
        )
        self._build_ui()

    def _take_queued_workers(self, workers: dict[object, QtCore.QRunnable]) -> None:
        try_take = getattr(self._pdf_thread_pool, "tryTake", None)
        if not callable(try_take):
            return
        for key, worker in tuple(workers.items()):
            if not isinstance(worker, QtCore.QRunnable):
                continue
            try:
                removed = try_take(worker)
            except RuntimeError:
                workers.pop(key, None)
                continue
            if removed:
                workers.pop(key, None)

    def _clear_queued_pdf_workers(self) -> None:
        self._take_queued_workers(self._pdf_scan_workers)
        self._take_queued_workers(self._pdf_manifest_workers)
        self._take_queued_workers(self._pdf_branch_workers)

    @property
    def files_tree(self) -> QtWidgets.QTreeWidget:
        return self._files_tree

    @property
    def files_stack(self) -> QtWidgets.QStackedWidget:
        return self._files_stack

    @property
    def pdf_folder_card(self) -> DropFolderCard:
        return self._pdf_folder_card

    @property
    def pdf_scan_status(self) -> PdfScanStatusPanel:
        return self._pdf_scan_status

    @property
    def pdf_counter_label(self) -> QtWidgets.QLabel:
        return self._pdf_counter_label

    @property
    def files_drop_zone(self) -> FilesDropZone:
        return self._files_drop_zone

    def _space(self, key: str, default: int) -> int:
        return int(self.theme.tokens.get("spacing", {}).get(key, default))

    def _build_ui(self) -> None:
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._pdf_scan_status = PdfScanStatusPanel()
        self._pdf_scan_status.cancel_requested.connect(self.cancel_scan)
        layout.addWidget(self._pdf_scan_status)

        self._files_stack = QtWidgets.QStackedWidget()

        empty_page = QtWidgets.QWidget()
        empty_page.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Expanding,
        )
        empty_layout = QtWidgets.QVBoxLayout(empty_page)
        card_margin = self._space("lg", 12)
        empty_layout.setContentsMargins(card_margin, card_margin, card_margin, card_margin)
        self._pdf_folder_card = DropFolderCard(
            on_click=self.folder_browse_requested.emit,
            on_drop=self.paths_dropped.emit,
        )
        self._pdf_folder_card.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Expanding,
        )
        card_layout = QtWidgets.QVBoxLayout(self._pdf_folder_card)
        card_layout.setSpacing(self._space("sm", 6))
        icon = QtWidgets.QLabel("📂")
        icon.setAlignment(QtCore.Qt.AlignCenter)
        icon.setObjectName("dropFolderIcon")
        title = QtWidgets.QLabel("Drag & drop PDFs")
        title.setAlignment(QtCore.Qt.AlignCenter)
        title.setObjectName("dropFolderTitle")
        subtitle = QtWidgets.QLabel("Drop a folder with PDFs\nor click to browse")
        subtitle.setAlignment(QtCore.Qt.AlignCenter)
        subtitle.setObjectName("dropFolderSubtitle")
        card_layout.addStretch(1)
        card_layout.addWidget(icon)
        card_layout.addWidget(title)
        card_layout.addWidget(subtitle)
        card_layout.addStretch(1)
        empty_layout.addWidget(self._pdf_folder_card, 1)
        self._files_stack.addWidget(empty_page)

        self._files_tree = QtWidgets.QTreeWidget()
        self._files_tree.setHeaderHidden(True)
        self._files_tree.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        self._files_tree.setTextElideMode(QtCore.Qt.ElideNone)
        self._files_tree.header().setStretchLastSection(False)
        self._files_tree.itemChanged.connect(self._on_files_tree_item_changed)
        self._files_tree.itemDoubleClicked.connect(self._on_files_tree_item_double_clicked)

        list_page = QtWidgets.QWidget()
        list_layout = QtWidgets.QVBoxLayout(list_page)
        list_layout.setContentsMargins(0, 0, 0, 0)
        self._files_drop_zone = FilesDropZone(self._files_tree, on_drop=self.paths_dropped.emit)
        list_layout.addWidget(self._files_drop_zone, 1)

        self._pdf_counter_label = QtWidgets.QLabel("Selected PDFs: 0")
        self._pdf_counter_label.setObjectName("subtitleText")
        list_layout.addWidget(self._pdf_counter_label)

        self._files_stack.addWidget(list_page)
        self._files_stack.setCurrentWidget(empty_page)
        layout.addWidget(self._files_stack, 1)

    def load_folder(self, folder: str) -> None:
        if not folder or not os.path.isdir(folder):
            self.clear_selection()
            return
        self.start_scan([folder], synthetic_root_name=None)

    def drop_paths(self, paths: list[str], *, preview_first: bool) -> None:
        self.start_scan(
            [os.path.abspath(path) for path in paths],
            synthetic_root_name="Dropped PDFs",
            preview_first=preview_first,
        )

    def clear_selection(self) -> None:
        if self._shutdown:
            return
        self.selection.cancel()
        self._clear_queued_pdf_workers()
        self._pdf_tree_timer.stop()
        self._pdf_stall_timer.stop()
        self._pdf_tree_queue.clear()
        self._pdf_branch_items.clear()
        self._pdf_group_items.clear()
        self.selection.replace_selected_paths({})
        self.selection.clear_warnings()
        previous = self._files_tree.blockSignals(True)
        try:
            self._files_tree.clear()
        finally:
            self._files_tree.blockSignals(previous)
        self._files_tree.setEnabled(True)
        self._pdf_scan_status.spinner.stop()
        self._pdf_scan_status.hide()
        self.update_pdf_counter()
        self._set_files_view_state(False)
        self.state_changed.emit()

    def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        self.selection.cancel()
        self._pdf_tree_queue.clear()
        self._pdf_tree_timer.stop()
        self._pdf_stall_timer.stop()
        # Includes the status panel's completion-hide and spinner timers.
        for timer in self._pdf_scan_status.findChildren(QtCore.QTimer):
            timer.stop()
        self._pdf_scan_status.spinner.stop()
        self._clear_queued_pdf_workers()
        # Do not wait on filesystem I/O here. Keep worker references until
        # terminal delivery; the child pool also outlives its running work.

    def start_scan(
        self,
        sources: list[str],
        synthetic_root_name: str | None,
        *,
        preview_first: bool = False,
    ) -> None:
        if self._shutdown:
            return
        self._clear_queued_pdf_workers()
        self._pdf_tree_timer.stop()

        request_id = self.selection.start(
            is_drop=synthetic_root_name is not None,
            preview_first=preview_first,
        )
        self._pdf_tree_build_request_id = request_id
        self._pdf_tree_queue.clear()
        self._pdf_tree_max_width = 0
        self._pdf_branch_items.clear()
        self._pdf_group_items.clear()
        self._pdf_stall_timer.stop()

        previous = self._files_tree.blockSignals(True)
        try:
            self._files_tree.clear()
        finally:
            self._files_tree.blockSignals(previous)
        self._files_tree.setEnabled(True)
        self._pdf_counter_label.setText("Selected PDFs: 0")
        self._set_files_view_state(True)
        self._pdf_scan_status.set_scanning(0, 0, [])
        self.state_changed.emit()

        worker = PdfManifestWorker(
            request_id,
            sources,
            synthetic_root_name,
        )
        worker.signals.result.connect(self._on_pdf_manifest_result)
        worker.signals.failed.connect(self._on_pdf_manifest_failed)
        self._pdf_manifest_workers[request_id] = worker
        self._pdf_thread_pool.start(worker)

    def _ensure_pdf_group(
        self,
        labels: tuple[str, ...],
        synthetic_root: QtWidgets.QTreeWidgetItem | None,
    ) -> QtWidgets.QTreeWidgetItem | None:
        parent = synthetic_root
        prefix: tuple[str, ...] = ()
        for label in labels:
            prefix += (label,)
            item = self._pdf_group_items.get(prefix)
            if item is None:
                item = QtWidgets.QTreeWidgetItem([label])
                item.setData(0, self.PDF_BRANCH_STATE_ROLE, "group")
                item.setFlags(item.flags() & ~QtCore.Qt.ItemIsUserCheckable)
                if parent is None:
                    self._files_tree.addTopLevelItem(item)
                else:
                    parent.addChild(item)
                self._pdf_group_items[prefix] = item
            parent = item
        return parent

    @QtCore.Slot(int, object)
    def _on_pdf_manifest_result(self, request_id: int, manifest: PdfScanManifest) -> None:
        self._pdf_manifest_workers.pop(request_id, None)
        if self._shutdown or not self.selection.accept_manifest(request_id, manifest):
            return
        self._pdf_tree_build_request_id = request_id
        previous = self._files_tree.blockSignals(True)
        try:
            self._files_tree.clear()
            self._pdf_group_items.clear()
            synthetic_root = None
            if manifest.root_name:
                synthetic_root = QtWidgets.QTreeWidgetItem([manifest.root_name])
                synthetic_root.setData(0, self.PDF_BRANCH_STATE_ROLE, "group")
                synthetic_root.setFlags(
                    synthetic_root.flags() & ~QtCore.Qt.ItemIsUserCheckable
                )
                self._files_tree.addTopLevelItem(synthetic_root)
                self._pdf_group_items[()] = synthetic_root

            for labels in manifest.empty_groups:
                item = self._ensure_pdf_group(labels, synthetic_root)
                if item is not None:
                    item.setText(0, f"{labels[-1]} (No PDFs)")
                    item.setData(0, self.PDF_BRANCH_STATE_ROLE, "empty")
                    self._set_pdf_item_foreground(item, "empty")

            for branch in manifest.branches:
                parent = self._ensure_pdf_group(branch.parent_labels, synthetic_root)
                item = QtWidgets.QTreeWidgetItem([f"{branch.name} (Scanning...)"])
                item.setFlags(item.flags() & ~QtCore.Qt.ItemIsUserCheckable)
                item.setData(0, self.PDF_BRANCH_ROLE, branch.branch_id)
                item.setData(0, self.PDF_BRANCH_STATE_ROLE, "pending")
                item.setData(0, self.PDF_TOTAL_ROLE, 0)
                item.setData(0, self.PDF_SELECTED_ROLE, 0)
                self._set_pdf_item_foreground(item, "pending")
                if parent is None:
                    self._files_tree.addTopLevelItem(item)
                else:
                    parent.addChild(item)
                self._pdf_branch_items[branch.branch_id] = item
        finally:
            self._files_tree.blockSignals(previous)

        self._files_tree.expandToDepth(1)
        if self.selection.pending_branch_ids:
            self._pdf_stall_timer.start()
            for branch in manifest.branches:
                self._submit_pdf_branch_worker(request_id, branch)
            self._refresh_pdf_scan_ui()
            return
        self._finish_concurrent_pdf_scan()

    def _submit_pdf_branch_worker(self, request_id: int, branch: PdfScanBranch) -> None:
        cancel_event = self.selection.cancel_event
        if cancel_event is None:
            return
        worker = PdfBranchWorker(
            request_id,
            branch,
            cancel_event,
        )
        worker.signals.progress.connect(self._on_pdf_branch_progress)
        worker.signals.result.connect(self._on_pdf_branch_result)
        worker.signals.failed.connect(self._on_pdf_branch_failed)
        self._pdf_branch_workers[(request_id, branch.branch_id)] = worker
        self._pdf_thread_pool.start(worker)

    @QtCore.Slot(int, str, int)
    def _on_pdf_branch_progress(
        self,
        request_id: int,
        branch_id: str,
        found_count: int,
    ) -> None:
        if not self.selection.update_branch_progress(
            request_id,
            branch_id,
            found_count,
        ):
            return
        self._refresh_pdf_scan_ui()

    @QtCore.Slot(int, str, object)
    def _on_pdf_branch_result(
        self,
        request_id: int,
        branch_id: str,
        result: PdfScanResult,
    ) -> None:
        self._pdf_branch_workers.pop((request_id, branch_id), None)
        state = self.selection.accept_branch_result(
            request_id,
            branch_id,
            result,
        )
        if state is None:
            return
        if state == "cancelled":
            self._render_pdf_branch_terminal(branch_id, state)
            self._refresh_pdf_group_states()
            self._refresh_pdf_scan_ui()
            if not self.selection.pending_branch_ids and not self._pdf_tree_queue:
                self._finish_concurrent_pdf_scan()
            return

        item = self._pdf_branch_items[branch_id]
        branch = self.selection.branches[branch_id]
        if result.warnings:
            item.setToolTip(0, "\n".join(result.warnings))

        previous = self._files_tree.blockSignals(True)
        try:
            if state == "empty":
                item.setData(0, self.PDF_BRANCH_STATE_ROLE, "empty")
                item.setText(0, f"{branch.name} (No PDFs)")
                item.setFlags(item.flags() & ~QtCore.Qt.ItemIsUserCheckable)
                self._set_pdf_item_foreground(item, "empty")
            else:
                root = result.roots[0]
                item.setData(0, self.PDF_BRANCH_STATE_ROLE, "complete")
                item.setText(0, branch.name)
                item.setFlags(item.flags() | QtCore.Qt.ItemIsUserCheckable)
                item.setData(0, self.PDF_TOTAL_ROLE, root.total_pdf_count)
                item.setData(0, self.PDF_SELECTED_ROLE, root.selected_pdf_count)
                if root.path is not None:
                    item.setData(0, self.PDF_PATH_ROLE, str(root.path))
                item.setCheckState(
                    0,
                    self._pdf_check_state(
                        root.total_pdf_count,
                        root.selected_pdf_count,
                    ),
                )
                self._set_pdf_item_foreground(item, "complete")
                self._pdf_tree_queue.extend(
                    (item, child, 1) for child in root.children
                )
                item.setExpanded(bool(root.children))
        finally:
            self._files_tree.blockSignals(previous)

        self._measure_pdf_tree_item_width(item)
        self._apply_pdf_tree_column_width()
        self._refresh_pdf_group_states()
        self._refresh_pdf_scan_ui()
        if self._pdf_tree_queue:
            self._pdf_tree_timer.start(0)
        elif not self.selection.pending_branch_ids:
            self._finish_concurrent_pdf_scan()

    @QtCore.Slot(int, str, str)
    def _on_pdf_branch_failed(
        self,
        request_id: int,
        branch_id: str,
        message: str,
    ) -> None:
        self._pdf_branch_workers.pop((request_id, branch_id), None)
        if not self.selection.fail_branch(request_id, branch_id):
            return
        item = self._pdf_branch_items[branch_id]
        item.setToolTip(0, message)
        self._render_pdf_branch_terminal(branch_id, "failed")
        self._refresh_pdf_group_states()
        self._refresh_pdf_scan_ui()
        if not self.selection.pending_branch_ids and not self._pdf_tree_queue:
            self._finish_concurrent_pdf_scan()

    @QtCore.Slot(int, str)
    def _on_pdf_manifest_failed(self, request_id: int, message: str) -> None:
        self._pdf_manifest_workers.pop(request_id, None)
        if self._shutdown or not self.selection.manifest_failed(request_id):
            return
        self._pdf_stall_timer.stop()
        self._files_tree.clear()
        self._set_files_view_state(False)
        self._pdf_scan_status.set_failed(message)
        self.state_changed.emit()

    def _render_pdf_branch_terminal(self, branch_id: str, state: str) -> None:
        item = self._pdf_branch_items[branch_id]
        branch = self.selection.branches[branch_id]
        suffix = {
            "cancelled": "Cancelled",
            "failed": "Scan failed",
            "empty": "No PDFs",
        }[state]
        previous = self._files_tree.blockSignals(True)
        try:
            item.setData(0, self.PDF_BRANCH_STATE_ROLE, state)
            item.setText(0, f"{branch.name} ({suffix})")
            item.setFlags(item.flags() & ~QtCore.Qt.ItemIsUserCheckable)
            self._set_pdf_item_foreground(item, state)
        finally:
            self._files_tree.blockSignals(previous)

    def _set_pdf_item_foreground(
        self,
        item: QtWidgets.QTreeWidgetItem,
        state: str,
    ) -> None:
        colors = self.theme.tokens.get("colors", {})
        if state == "pending":
            color = QtGui.QColor(colors.get("warning", "#ffb74d"))
        elif state == "stalled":
            color = QtGui.QColor(colors.get("warning", "#ffb74d")).darker(135)
        elif state == "failed":
            color = QtGui.QColor(colors.get("accent_soft", "#ff6b6b"))
        elif state in {"empty", "cancelled"}:
            color = QtGui.QColor(colors.get("text_muted", "#b9b9b9"))
        else:
            color = self._files_tree.palette().color(QtGui.QPalette.Text)
        item.setForeground(0, QtGui.QBrush(color))

    def _refresh_pdf_group_states(self) -> None:
        previous = self._files_tree.blockSignals(True)
        try:
            groups = sorted(
                self._pdf_group_items.items(),
                key=lambda pair: len(pair[0]),
                reverse=True,
            )
            for _, item in groups:
                total = sum(
                    int(item.child(index).data(0, self.PDF_TOTAL_ROLE) or 0)
                    for index in range(item.childCount())
                )
                selected = sum(
                    int(item.child(index).data(0, self.PDF_SELECTED_ROLE) or 0)
                    for index in range(item.childCount())
                )
                pending = any(
                    self._pdf_item_has_pending(item.child(index))
                    for index in range(item.childCount())
                )
                item.setData(0, self.PDF_TOTAL_ROLE, total)
                item.setData(0, self.PDF_SELECTED_ROLE, selected)
                if pending or total == 0:
                    item.setFlags(item.flags() & ~QtCore.Qt.ItemIsUserCheckable)
                else:
                    item.setFlags(item.flags() | QtCore.Qt.ItemIsUserCheckable)
                    item.setCheckState(0, self._pdf_check_state(total, selected))
        finally:
            self._files_tree.blockSignals(previous)

    def _pdf_item_has_pending(self, item: QtWidgets.QTreeWidgetItem) -> bool:
        state = item.data(0, self.PDF_BRANCH_STATE_ROLE)
        if state in {"pending", "stalled"}:
            return True
        return any(
            self._pdf_item_has_pending(item.child(index))
            for index in range(item.childCount())
        )

    def _update_pdf_stalled_states(self) -> None:
        if not self.selection.pending_branch_ids:
            self._pdf_stall_timer.stop()
            return
        for branch_id in self.selection.mark_stalled():
            branch = self.selection.branches[branch_id]
            item = self._pdf_branch_items[branch_id]
            item.setData(0, self.PDF_BRANCH_STATE_ROLE, "stalled")
            item.setText(0, f"{branch.name} (Still scanning...)")
            self._set_pdf_item_foreground(item, "stalled")
        self._refresh_pdf_scan_ui()

    def cancel_scan(self) -> None:
        if not self.selection.active:
            return
        self._clear_queued_pdf_workers()
        for branch_id in self.selection.cancel():
            self._render_pdf_branch_terminal(branch_id, "cancelled")
        self._pdf_tree_queue.clear()
        self._pdf_tree_timer.stop()
        self._pdf_stall_timer.stop()
        self._refresh_pdf_group_states()
        self._pdf_scan_status.set_cancelled(self.selection.found_count)
        self.update_pdf_counter()
        self.state_changed.emit()

    def _refresh_pdf_scan_ui(self) -> None:
        found_count = self.selection.found_count
        if self.selection.pending_branch_ids:
            stalled_names = [
                self.selection.branches[branch_id].name
                for branch_id in self.selection.branches
                if self.selection.branch_states.get(branch_id) == "stalled"
            ]
            self._pdf_scan_status.set_scanning(
                found_count,
                len(self.selection.pending_branch_ids),
                stalled_names,
            )
        self.update_pdf_counter()
        self.state_changed.emit()

    def _finish_concurrent_pdf_scan(self) -> None:
        if self._shutdown or not self.selection.active:
            return
        if not self.selection.finish_if_ready(
            tree_pending=bool(self._pdf_tree_queue)
        ):
            return
        self._apply_pdf_tree_column_width()
        self._pdf_stall_timer.stop()
        self._refresh_pdf_group_states()
        found_count = self.selection.found_count
        if self.selection.cancelled:
            self._pdf_scan_status.set_cancelled(found_count)
        elif self.selection.had_failure:
            failed_count = sum(
                state == "failed" for state in self.selection.branch_states.values()
            )
            self._pdf_scan_status.set_incomplete(
                found_count,
                failed_count,
            )
        else:
            self._pdf_scan_status.set_complete(found_count)
        self.update_pdf_counter()
        self.state_changed.emit()
        self.scan_finished.emit()

    @QtCore.Slot(int, int)
    def _on_pdf_scan_progress(self, request_id: int, found_count: int) -> None:
        if self._shutdown or not self.selection.active or not self.selection.is_current(request_id):
            return
        self._pdf_counter_label.setText(f"Scanning PDFs... Found {found_count}")

    @QtCore.Slot(int, object)
    def _on_pdf_scan_result(self, request_id: int, result: PdfScanResult) -> None:
        if (
            self._shutdown
            or not self.selection.active
            or not self.selection.accept_scan_result(request_id, result)
        ):
            self._pdf_scan_workers.pop(request_id, None)
            return
        self._pdf_scan_workers.pop(request_id, None)
        if result.cancelled:
            self._files_tree.setEnabled(True)
            self.state_changed.emit()
            return
        self._begin_pdf_tree_build(request_id, result.roots)

    @QtCore.Slot(int, str)
    def _on_pdf_scan_failed(self, request_id: int, message: str) -> None:
        self._pdf_scan_workers.pop(request_id, None)
        if not self.selection.manifest_failed(request_id):
            return
        self._files_tree.setEnabled(True)
        self._pdf_counter_label.setText("Selected PDFs: 0")
        self._set_files_view_state(False)
        QtWidgets.QMessageBox.critical(
            self,
            "PDF Scan Error",
            f"Could not scan the selected PDF sources.\n\n{message}",
        )
        self.state_changed.emit()

    def _begin_pdf_tree_build(self, request_id: int, roots: list[PdfTreeNode]) -> None:
        if self._shutdown or not self.selection.active or not self.selection.is_current(request_id):
            return
        self._pdf_tree_build_request_id = request_id
        self._pdf_tree_queue.clear()
        self._pdf_tree_queue.extend((None, root, 0) for root in roots)
        self._pdf_tree_max_width = 0
        previous = self._files_tree.blockSignals(True)
        try:
            self._files_tree.clear()
        finally:
            self._files_tree.blockSignals(previous)
        if not self._pdf_tree_queue:
            self._finish_pdf_tree_build(request_id)
            return
        self._pdf_tree_timer.start(0)

    @staticmethod
    def _pdf_check_state(total: int, selected: int) -> QtCore.Qt.CheckState:
        if selected <= 0:
            return QtCore.Qt.Unchecked
        if selected >= total:
            return QtCore.Qt.Checked
        return QtCore.Qt.PartiallyChecked

    def _measure_pdf_tree_item_width(
        self,
        item: QtWidgets.QTreeWidgetItem,
        depth: int | None = None,
    ) -> None:
        if depth is None:
            depth = 0
            parent = item.parent()
            while parent is not None:
                depth += 1
                parent = parent.parent()
        width = (
            self._files_tree.fontMetrics().horizontalAdvance(item.text(0))
            + (depth * self._files_tree.indentation())
            + 32
        )
        self._pdf_tree_max_width = max(self._pdf_tree_max_width, width)

    def _apply_pdf_tree_column_width(self) -> None:
        if self._pdf_tree_max_width:
            self._files_tree.setColumnWidth(0, min(self._pdf_tree_max_width, 1200))

    def _continue_pdf_tree_build(self, request_id: int) -> None:
        if (
            self._shutdown
            or not self.selection.active
            or not self.selection.is_current(request_id)
            or request_id != self._pdf_tree_build_request_id
        ):
            return

        elapsed = QtCore.QElapsedTimer()
        elapsed.start()
        previous = self._files_tree.blockSignals(True)
        self._files_tree.setUpdatesEnabled(False)
        try:
            processed = 0
            while self._pdf_tree_queue and (
                processed == 0 or elapsed.elapsed() < self.PDF_TREE_TIME_BUDGET_MS
            ):
                parent_item, node, depth = self._pdf_tree_queue.popleft()
                processed += 1
                item = QtWidgets.QTreeWidgetItem([node.name])
                item.setFlags(item.flags() | QtCore.Qt.ItemIsUserCheckable)
                if node.path is not None:
                    item.setData(0, self.PDF_PATH_ROLE, str(node.path))
                item.setData(0, self.PDF_TOTAL_ROLE, node.total_pdf_count)
                item.setData(0, self.PDF_SELECTED_ROLE, node.selected_pdf_count)
                item.setCheckState(
                    0,
                    self._pdf_check_state(node.total_pdf_count, node.selected_pdf_count),
                )
                if parent_item is None:
                    self._files_tree.addTopLevelItem(item)
                else:
                    parent_item.addChild(item)
                self._pdf_tree_queue.extend(
                    (item, child, depth + 1) for child in node.children
                )
                if node.children:
                    item.setExpanded(True)
                self._measure_pdf_tree_item_width(item)
        finally:
            self._files_tree.setUpdatesEnabled(True)
            self._files_tree.blockSignals(previous)

        self._apply_pdf_tree_column_width()

        if self._pdf_tree_queue:
            self._pdf_tree_timer.start(0)
        elif self.selection.branches:
            self._refresh_pdf_group_states()
            if not self.selection.pending_branch_ids:
                self._finish_concurrent_pdf_scan()
        else:
            self._finish_pdf_tree_build(request_id)

    def _finish_pdf_tree_build(self, request_id: int) -> None:
        if self._shutdown or not self.selection.active or not self.selection.is_current(request_id):
            return
        self._apply_pdf_tree_column_width()
        if self._files_tree.topLevelItemCount():
            self._files_tree.topLevelItem(0).setExpanded(True)

        self.selection.finish_if_ready(tree_pending=False)
        self._files_tree.setEnabled(True)
        has_files = self._files_tree.topLevelItemCount() > 0
        self._set_files_view_state(has_files)
        self.update_pdf_counter()

        if not has_files and self.selection.is_drop:
            QtWidgets.QMessageBox.critical(
                self,
                "Invalid Drop",
                "No PDF files found in the dropped folders or files.",
            )
        self.state_changed.emit()
        self.scan_finished.emit()

    def _on_files_tree_item_changed(self, item: QtWidgets.QTreeWidgetItem, _column: int):
        state = item.checkState(0)
        if state == QtCore.Qt.PartiallyChecked:
            return

        generation = self.selection.selection_generation
        previous = self._files_tree.blockSignals(True)
        try:
            stack = [item]
            while stack:
                current = stack.pop()
                total = int(current.data(0, self.PDF_TOTAL_ROLE) or 0)
                selected = total if state == QtCore.Qt.Checked else 0
                current.setCheckState(0, state)
                current.setData(0, self.PDF_SELECTED_ROLE, selected)
                path = current.data(0, self.PDF_PATH_ROLE)
                if path:
                    self.selection.set_selected_path(
                        path,
                        state == QtCore.Qt.Checked,
                    )
                for index in range(current.childCount()):
                    stack.append(current.child(index))

            parent = item.parent()
            while parent is not None:
                total = sum(
                    int(parent.child(index).data(0, self.PDF_TOTAL_ROLE) or 0)
                    for index in range(parent.childCount())
                )
                selected = sum(
                    int(parent.child(index).data(0, self.PDF_SELECTED_ROLE) or 0)
                    for index in range(parent.childCount())
                )
                parent.setData(0, self.PDF_TOTAL_ROLE, total)
                parent.setData(0, self.PDF_SELECTED_ROLE, selected)
                parent.setCheckState(0, self._pdf_check_state(total, selected))
                parent = parent.parent()
        finally:
            self._files_tree.blockSignals(previous)
        self.update_pdf_counter()
        if self.selection.selection_generation == generation:
            return
        self.selection_changed.emit()
        self.state_changed.emit()

    def _on_files_tree_item_double_clicked(self, item: QtWidgets.QTreeWidgetItem, _column: int) -> None:
        path = item.data(0, self.PDF_PATH_ROLE)
        if path and os.path.isfile(path) and path.lower().endswith(".pdf"):
            self.preview_requested.emit(path)

    def update_pdf_counter(self):
        text = f"Selected PDFs: {len(self.selection.selected_paths)}"
        if self.selection.warnings:
            text += f" ({len(self.selection.warnings)} folders skipped)"
        self._pdf_counter_label.setText(text)

    def selected_paths_for_extraction(self) -> list[Path]:
        return self.selection.paths_for_extraction()

    def _set_files_view_state(self, show_files: bool) -> None:
        if not hasattr(self, "files_stack"):
            return
        target_widget = self._files_stack.widget(1) if show_files else self._files_stack.widget(0)
        if self._files_stack.currentWidget() is not target_widget:
            self._files_stack.setCurrentWidget(target_widget)
