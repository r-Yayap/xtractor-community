from __future__ import annotations

import os
from enum import Enum
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from app.common.packaging import is_packaged_application, packaged_root
from app.controllers.extract_controller import ExtractController
from app.controllers.extraction_resource_admission import ExtractionResourceAdmission
from app.controllers.extraction_run_controller import ExtractionRunController
from app.controllers.output_readiness_controller import OutputReadinessController
from app.domain.extraction_job import (
    ExtractionJobDiagnosticCode,
    ExtractionJobFailure,
    ExtractionJobState,
    ExtractionJobStatus,
)
from app.domain.extraction_progress import ExtractionProgressPhase
from app.domain.extraction_summary import ExtractionSummary
from app.domain.job_readiness import ReadinessState
from app.domain.models import ExtractionRequest, OcrSettings
from app.services.job_readiness_service import JobReadinessService
from app.ui.constants import SIDEBAR_WIDTH, SIDEBAR_WIDTH_MAX, VERSION_TEXT
from app.ui.help import HelpSurface, open_local_path
from app.ui.output_readiness import OutputReadinessWorker
from app.ui.pdf_selection_panel import PdfSelectionPanel
from app.ui.pdf_viewer import PDFViewer
from app.ui.readiness_components import ElidedSelectableLabel
from app.ui.theme import get_theme
from app.ui.window_chrome import FramelessMainWindow
from app.workflows.extraction import (
    ExtractionInputs,
    ExtractionPreparationFailure,
    ExtractionWorkflow,
)
from app.workflows.pdf_selection import PdfSelectionCoordinator


class _WindowShutdownState(Enum):
    OPEN = "open"
    STOPPING_EXTRACTION = "stopping_extraction"
    READY_TO_CLOSE = "ready_to_close"


class _ExtractionProgressDialog(QtWidgets.QDialog):
    cancel_requested = QtCore.Signal()

    def reject(self) -> None:
        self.cancel_requested.emit()


class _ExtractionShutdownThread(QtCore.QThread):
    def __init__(
        self,
        controller: ExtractionRunController,
        parent: QtCore.QObject,
    ) -> None:
        super().__init__(parent)
        self._controller = controller

    def run(self) -> None:
        self._controller.dispose()


_EXTRACTION_PHASE_LABELS = {
    ExtractionProgressPhase.PREPARING: "Preparing extraction…",
    ExtractionProgressPhase.COMBINING: "Combining extracted data…",
    ExtractionProgressPhase.WRITING_EXCEL: "Writing Excel…",
    ExtractionProgressPhase.FINALIZING: "Finalizing…",
}


def resource_path(rel: str) -> str:
    candidate = packaged_root() / rel
    if is_packaged_application() or candidate.exists():
        return str(candidate)
    return str(Path(__file__).resolve().parent / rel)


def _format_elapsed_hms(elapsed_seconds: float | None) -> str | None:
    if elapsed_seconds is None:
        return None
    hours, remainder = divmod(int(max(0.0, elapsed_seconds)), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _render_extraction_progress(
    state: ExtractionJobState,
    progress_label: QtWidgets.QLabel,
    total_label: QtWidgets.QLabel,
    recent_label: ElidedSelectableLabel,
    bar: QtWidgets.QProgressBar,
) -> None:
    progress = state.progress
    if state.status is ExtractionJobStatus.CANCELLING:
        progress_label.setText("Cancelling…")
    elif progress.phase is ExtractionProgressPhase.EXTRACTING:
        percentage = progress.percentage
        if percentage is None:
            progress_label.setText("Extracting PDFs…")
        else:
            progress_label.setText(
                "Extracting PDFs… "
                f"({progress.processed_files} / {progress.total_files}, {percentage}%)"
            )
    else:
        progress_label.setText(_EXTRACTION_PHASE_LABELS[progress.phase])

    details: list[str] = []
    if progress.total_files > 0:
        details.append(
            f"Files: {progress.processed_files} / {progress.total_files}"
        )
    details.append(f"Succeeded: {progress.succeeded_files}")
    details.append(f"Errors: {progress.failed_files}")
    if progress.processed_pages is not None:
        details.append(f"Pages: {progress.processed_pages}")
    formatted_elapsed = _format_elapsed_hms(state.elapsed_seconds)
    if formatted_elapsed is not None:
        details.append(f"Elapsed: {formatted_elapsed}")
    if progress.ocr_enabled:
        details.append("OCR enabled")
    total_label.setText(" • ".join(details))

    if progress.recent_file is None:
        recent_label.setFullText("")
    else:
        recent_label.setFullText(f"Last completed: {progress.recent_file}")

    if (
        state.status is not ExtractionJobStatus.CANCELLING
        and progress.phase is ExtractionProgressPhase.EXTRACTING
        and progress.total_files > 0
    ):
        bar.setRange(0, progress.total_files)
        bar.setValue(progress.processed_files)
        return
    bar.setRange(0, 0)


class XtractorGUI(FramelessMainWindow):
    def __init__(
        self,
        *,
        extraction_admission: ExtractionResourceAdmission | None = None,
    ):
        super().__init__(
            visible_title="Xtractor",
            logo_path=resource_path("style/Xtractor-Logo.svg"),
            control_icon_paths={
                "minimize": resource_path("style/icons/title-minimize.svg"),
                "maximize": resource_path("style/icons/title-maximize.svg"),
                "close": resource_path("style/icons/title-close.svg"),
            },
        )
        self._shutdown_state = _WindowShutdownState.OPEN
        self._shutdown_thread: _ExtractionShutdownThread | None = None
        self._active_extraction_progress_dialog: QtWidgets.QDialog | None = None
        self._active_extraction_progress_label: QtWidgets.QLabel | None = None
        self._active_extraction_cancel_button: QtWidgets.QPushButton | None = None

        self.extraction_admission = extraction_admission or ExtractionResourceAdmission()
        self.extractor = ExtractController()
        self.extraction_run = ExtractionRunController(
            self.extractor, admission=self.extraction_admission
        )
        self.extraction_workflow = ExtractionWorkflow(JobReadinessService())
        self.output_readiness = OutputReadinessController()
        self._output_probe_thread_pool = QtCore.QThreadPool(self)
        self._output_probe_thread_pool.setMaxThreadCount(2)
        self._output_probe_workers: dict[int, OutputReadinessWorker] = {}
        self._output_probe_timer = QtCore.QTimer(self, singleShot=True, interval=300)
        self._output_probe_timer.timeout.connect(self._start_output_probe)

        self.help_surface = HelpSurface(self, self.btn_help)
        self.theme = get_theme()
        self.pdf_folder = ""
        self.output_excel_path = ""
        self.recent_pdf_path: str | None = None
        self._area_edit_validation_error: str | None = None
        self.pdf_selection = PdfSelectionCoordinator()
        self.pdf_viewer = PDFViewer(self)
        self.zoom_slider = self.pdf_viewer.zoom_slider
        self.zoom_slider.valueChanged.connect(self.update_zoom)

        self.setWindowTitle(f"Xtractor {VERSION_TEXT}")
        self.setWindowIcon(self._load_app_icon())
        self.resize(965, 720)
        self.setup_widgets()

    def setup_widgets(self):
        central = QtWidgets.QWidget(self)
        central.setObjectName("XtractorWorkspace")
        self.set_window_body(central)
        layout = QtWidgets.QHBoxLayout(central)
        margin = self._space("md", 8)
        layout.setContentsMargins(margin, margin, margin, margin)

        self.main_splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.main_splitter.setChildrenCollapsible(False)
        layout.addWidget(self.main_splitter)

        self.tab_files = QtWidgets.QWidget()
        self._build_files_tab()
        sidebar = QtWidgets.QWidget()
        sidebar.setMinimumWidth(SIDEBAR_WIDTH)
        sidebar.setMaximumWidth(SIDEBAR_WIDTH_MAX)
        sidebar_layout = QtWidgets.QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        browse_folder = QtWidgets.QPushButton("Browse Folder…")
        browse_folder.clicked.connect(self.browse_pdf_folder)
        sidebar_layout.addWidget(browse_folder)
        sidebar_layout.addWidget(self.tab_files, 1)

        areas_box = QtWidgets.QGroupBox("Extraction areas")
        areas_layout = QtWidgets.QVBoxLayout(areas_box)
        instructions = QtWidgets.QLabel("Draw a box around each field in the PDF.")
        instructions.setWordWrap(True)
        areas_layout.addWidget(instructions)
        self.areas_tree = QtWidgets.QListWidget()
        self.areas_tree.setAccessibleName("Extraction areas")
        self.areas_tree.setMaximumHeight(112)
        self.areas_tree.currentRowChanged.connect(self.on_treeview_select)
        areas_layout.addWidget(self.areas_tree)
        self.clear_areas_button = QtWidgets.QPushButton("Clear areas")
        self.clear_areas_button.clicked.connect(self.pdf_viewer.remove_all_areas)
        areas_layout.addWidget(self.clear_areas_button)
        sidebar_layout.addWidget(areas_box)

        self.ocr_checkbox = QtWidgets.QCheckBox("Use OCR for scanned PDFs")
        sidebar_layout.addWidget(self.ocr_checkbox)
        output_row = QtWidgets.QHBoxLayout()
        self.output_path_entry = QtWidgets.QLineEdit()
        self.output_path_entry.setPlaceholderText("Excel output path")
        self.output_path_entry.textChanged.connect(self._on_output_path_changed)
        output_row.addWidget(self.output_path_entry, 1)
        self.browse_output_button = QtWidgets.QPushButton("Save to…")
        self.browse_output_button.clicked.connect(self.browse_output_path)
        output_row.addWidget(self.browse_output_button)
        sidebar_layout.addLayout(output_row)

        self.output_status = QtWidgets.QLabel()
        self.output_status.setWordWrap(True)
        self.output_status.hide()
        sidebar_layout.addWidget(self.output_status)
        self.extract_button = QtWidgets.QPushButton("Extract to Excel")
        self.extract_button.setProperty("variant", "primary")
        self.extract_button.clicked.connect(self.start_extraction)
        sidebar_layout.addWidget(self.extract_button)

        self.main_splitter.addWidget(sidebar)
        self.main_splitter.addWidget(self.pdf_viewer)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.setSizes([SIDEBAR_WIDTH, self.width() - SIDEBAR_WIDTH])
        self.update_areas_treeview()

    def _build_files_tab(self):
        layout = QtWidgets.QVBoxLayout(self.tab_files)
        self.pdf_selection_panel = PdfSelectionPanel(
            selection=self.pdf_selection,
            theme=self.theme,
            parent=self.tab_files,
        )
        self.pdf_selection_panel.folder_browse_requested.connect(self.browse_pdf_folder)
        self.pdf_selection_panel.paths_dropped.connect(self.drop_pdf_folder)
        self.pdf_selection_panel.state_changed.connect(self._sync_pdf_extraction_enabled)
        self.pdf_selection_panel.scan_finished.connect(self._on_pdf_selection_scan_finished)
        self.pdf_selection_panel.preview_requested.connect(self._preview_pdf)
        layout.addWidget(self.pdf_selection_panel)

    def _load_app_icon(self) -> QtGui.QIcon:
        for icon_name in ("Xtractor-Logo.ico", "Xtractor-Logo.png"):
            path = Path(resource_path(f"style/{icon_name}"))
            if path.exists():
                return QtGui.QIcon(str(path))
        return QtGui.QIcon()

    def _space(self, key: str, default: int) -> int:
        return int(self.theme.tokens.get("spacing", {}).get(key, default))

    @property
    def files_tree(self):
        return self.pdf_selection_panel.files_tree

    @property
    def files_stack(self):
        return self.pdf_selection_panel.files_stack

    @property
    def pdf_folder_card(self):
        return self.pdf_selection_panel.pdf_folder_card

    @property
    def pdf_scan_status(self):
        return self.pdf_selection_panel.pdf_scan_status

    @property
    def pdf_counter_label(self):
        return self.pdf_selection_panel.pdf_counter_label

    def close_pdf(self):
        self.pdf_viewer.close_pdf()

    def update_zoom_slider(self, zoom_level):
        self.zoom_slider.blockSignals(True)
        self.zoom_slider.setValue(int(zoom_level * 100))
        self.zoom_slider.blockSignals(False)

    def update_zoom(self, value):
        self.pdf_viewer.set_zoom(value / 100.0)

    def browse_pdf_folder(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Select PDF Folder")
        if not folder:
            return
        self.pdf_folder = folder
        self._start_pdf_scan([folder], synthetic_root_name=None)

    def browse_output_path(self):
        output_path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Save Excel",
            filter="Excel files (*.xlsx)",
        )
        if output_path:
            self.output_excel_path = output_path
            self.output_path_entry.setText(output_path)

    def build_folder_tree(self):
        self.pdf_selection_panel.load_folder(self.pdf_folder)

    def _start_pdf_scan(
        self,
        sources: list[str],
        synthetic_root_name: str | None,
        *,
        preview_first: bool = False,
    ) -> None:
        self.pdf_selection_panel.start_scan(
            sources, synthetic_root_name, preview_first=preview_first,
        )

    def _selected_paths_for_extraction(self) -> list[Path]:
        return self.pdf_selection_panel.selected_paths_for_extraction()

    def drop_pdf_folder(self, paths):
        cleaned_paths = [os.path.abspath(p) for p in paths]
        self.pdf_folder = ""
        self.pdf_selection_panel.drop_paths(
            cleaned_paths, preview_first=not self.pdf_viewer.has_document,
        )

    def _on_output_path_changed(self, value: str) -> None:
        self.output_excel_path = value.strip()
        request = self.output_readiness.change_path(self.output_excel_path)
        self._output_probe_timer.stop()
        self._output_probe_thread_pool.clear()
        if request is not None:
            self._output_probe_timer.start()
        self._sync_pdf_extraction_enabled()

    @QtCore.Slot()
    def _start_output_probe(self) -> None:
        state = self.output_readiness.state
        if state.closing or state.path is None:
            return
        if state.path.suffix.casefold() != ".xlsx":
            return
        request_id = state.request_id
        worker = OutputReadinessWorker(
            request_id=request_id,
            output_path=state.path,
        )
        worker.signals.result.connect(self._on_output_probe_result)
        worker.signals.failed.connect(self._on_output_probe_failed)
        self._output_probe_workers[request_id] = worker
        self._output_probe_thread_pool.start(worker)

    @QtCore.Slot(int, object)
    def _on_output_probe_result(self, request_id: int, result) -> None:
        self._output_probe_workers.pop(request_id, None)
        if not self.output_readiness.accept_result(request_id, result):
            return
        self._sync_pdf_extraction_enabled()

    @QtCore.Slot(int, str)
    def _on_output_probe_failed(self, request_id: int, message: str) -> None:
        self._output_probe_workers.pop(request_id, None)
        if not self.output_readiness.accept_failure(request_id, message):
            return
        self._sync_pdf_extraction_enabled()

    def on_pdf_loaded(self, path: str) -> None:
        self.recent_pdf_path = path
        self.pdf_viewer.set_viewer_controls_state(True, True)

    def on_pdf_closed(self) -> None:
        self.pdf_viewer.set_viewer_controls_state(False, bool(self.recent_pdf_path))

    def _preview_pdf(self, path: str) -> None:
        try:
            self.pdf_viewer.display_pdf(path)
        except Exception:
            QtWidgets.QMessageBox.warning(self, "PDF preview", "The PDF could not be opened.")

    def open_recent_pdf(self) -> None:
        if self.recent_pdf_path:
            self._preview_pdf(self.recent_pdf_path)

    def update_areas_treeview(self) -> None:
        self._area_edit_validation_error = None
        self.areas_tree.clear()
        self.areas_tree.addItems([area.title for area in self.pdf_viewer.areas])
        self.clear_areas_button.setEnabled(bool(self.pdf_viewer.areas))
        self._sync_pdf_extraction_enabled()

    def on_treeview_select(self, row: int) -> None:
        self.pdf_viewer.highlight_rectangle(row if row >= 0 else None)

    def set_area_edit_validation_error(self, message: str) -> None:
        self._area_edit_validation_error = message
        self._sync_pdf_extraction_enabled()

    def _on_pdf_selection_scan_finished(self) -> None:
        if (
            not self.pdf_selection.cancelled
            and not self.pdf_selection.had_failure
            and self.pdf_selection.selected_paths
            and self.pdf_selection.preview_path
            and not self.pdf_viewer.has_document
        ):
            self._preview_pdf(self.pdf_selection.preview_path)

    def _sync_pdf_extraction_enabled(self) -> None:
        if self._shutdown_state is not _WindowShutdownState.OPEN:
            return
        scan_pending = bool(self.pdf_selection.pending_branch_ids)
        output = self.output_readiness.state
        probe = output.probe_result
        message = ""
        if probe is not None:
            if probe.section.state is ReadinessState.BLOCKED:
                message = probe.section.summary
            elif probe.plan is not None and probe.plan.collision:
                message = f"Existing file kept. Saving to {probe.plan.planned_path.name}."
        elif output.path is not None:
            if output.path.suffix.casefold() == ".xlsx":
                message = "Checking output folder…"
            else:
                message = "Choose an output filename ending in .xlsx."
        self.output_status.setText(self._area_edit_validation_error or message)
        self.output_status.setVisible(bool(self.output_status.text()))
        self.extract_button.setEnabled(
            not self.extraction_run.state.active
            and not scan_pending
            and bool(self.pdf_viewer.areas)
            and bool(self._selected_paths_for_extraction())
            and probe is not None
            and probe.section.state is ReadinessState.READY
        )

    def _extraction_inputs(self) -> ExtractionInputs:
        use_ocr = self.ocr_checkbox.isChecked()
        return ExtractionInputs(
            pdf_paths=self._selected_paths_for_extraction(),
            output_excel=self.output_excel_path,
            areas=self.pdf_viewer.get_gui_areas(),
            ocr=OcrSettings(
                mode="OCR-All" if use_ocr else "Default",
                dpi=150,
                enabled=use_ocr,
            ),
            coordinate_basis=self.pdf_viewer.coordinate_basis,
        )

    def _handle_successful_extraction(
        self, output_path: Path, summary: ExtractionSummary
    ) -> None:
        dialog = QtWidgets.QMessageBox(self)
        dialog.setWindowTitle("Extraction complete")
        dialog.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        dialog.setText(f"Saved {output_path}")
        dialog.setInformativeText(
            f"{summary.processed_pdfs - summary.errors} PDFs processed. {summary.errors} failed."
        )
        open_button = dialog.addButton("Open Excel", QtWidgets.QMessageBox.ActionRole)
        dialog.addButton(QtWidgets.QMessageBox.Close)
        dialog.exec()
        if dialog.clickedButton() is open_button and not open_local_path(output_path):
            QtWidgets.QMessageBox.warning(self, "Open Excel", "The workbook could not be opened.")

    def _show_extraction_preparation_failure(self, failure: ExtractionPreparationFailure) -> None:
        messages = {
            ExtractionPreparationFailure.NO_AREAS_DEFINED: "Draw at least one extraction area.",
            ExtractionPreparationFailure.NO_PDFS_SELECTED: "Select at least one PDF.",
            ExtractionPreparationFailure.INVALID_OUTPUT_PATH: "Choose a writable Excel output path.",
            ExtractionPreparationFailure.INVALID_AREA_GEOMETRY: "Redraw the area with invalid geometry.",
            ExtractionPreparationFailure.DUPLICATE_AREA_TITLES: "Remove the duplicate extraction area.",
        }
        QtWidgets.QMessageBox.warning(
            self, "Extraction setup", messages.get(failure, "Check the PDF and extraction areas.")
        )

    def start_extraction(self, *, prepared_request: ExtractionRequest | None = None):
        if getattr(
            self,
            "_shutdown_state",
            _WindowShutdownState.OPEN,
        ) is not _WindowShutdownState.OPEN:
            return
        if self.extraction_run.state.active:
            return
        if prepared_request is None:
            preparation = self.extraction_workflow.prepare(
                self._extraction_inputs(),
                output_probe_result=self.output_readiness.state.probe_result,
                probe_output_if_missing=False,
            )
            if preparation.failure is not None:
                self._show_extraction_preparation_failure(preparation.failure)
                return
            if preparation.prepared is None:
                raise RuntimeError("Extraction preparation returned no result")
            prepared_request = preparation.prepared.request
        req = prepared_request

        progress = _ExtractionProgressDialog(self)
        progress.setWindowTitle("Progress")
        progress.setWindowFlag(QtCore.Qt.WindowType.WindowCloseButtonHint, False)
        progress_layout = QtWidgets.QVBoxLayout(progress)
        progress_label = QtWidgets.QLabel()
        total_label = QtWidgets.QLabel()
        recent_label = ElidedSelectableLabel()
        screen = self.screen()
        recent_label.setMaximumWidth(
            min(640, max(1, int(screen.availableGeometry().width() * 0.8)))
            if screen is not None else 640
        )
        bar = QtWidgets.QProgressBar()
        bar.setRange(0, 0)
        cancel_btn = QtWidgets.QPushButton("Cancel")
        progress_layout.addWidget(progress_label)
        progress_layout.addWidget(total_label)
        progress_layout.addWidget(recent_label)
        progress_layout.addWidget(bar)
        progress_layout.addWidget(cancel_btn)

        state = self.extraction_run.start(req)
        self._sync_pdf_extraction_enabled()
        if not state.active:
            progress.accept()
            elapsed = state.elapsed_seconds or 0.0
            formatted = _format_elapsed_hms(elapsed)
            self._show_unsuccessful_extraction(state, formatted)
            return
        self._active_extraction_progress_dialog = progress
        self._active_extraction_progress_label = progress_label
        self._active_extraction_cancel_button = cancel_btn
        _render_extraction_progress(
            state,
            progress_label,
            total_label,
            recent_label,
            bar,
        )

        def tick():
            if getattr(
                self,
                "_shutdown_state",
                _WindowShutdownState.OPEN,
            ) is not _WindowShutdownState.OPEN:
                return
            state = self.extraction_run.poll()
            if state.active:
                _render_extraction_progress(
                    state,
                    progress_label,
                    total_label,
                    recent_label,
                    bar,
                )
                QtCore.QTimer.singleShot(100, tick)
            else:
                progress.accept()
                self._sync_pdf_extraction_enabled()
                elapsed = state.elapsed_seconds or 0.0
                formatted = _format_elapsed_hms(elapsed)
                if (
                    state.status
                    not in (
                        ExtractionJobStatus.COMPLETED,
                        ExtractionJobStatus.COMPLETED_WITH_WARNINGS,
                    )
                    or state.result is None
                ):
                    self._show_unsuccessful_extraction(state, formatted)
                    return
                self._handle_successful_extraction(
                    state.result.output_path,
                    state.result.summary,
                )

        cancel_btn.clicked.connect(
            lambda: self.on_cancel_extraction(
                progress_label,
                total_label,
                recent_label,
                bar,
                cancel_btn,
            )
        )

        progress.cancel_requested.connect(cancel_btn.click)

        QtCore.QTimer.singleShot(100, tick)
        try:
            progress.exec()
        finally:
            if self._active_extraction_progress_dialog is progress:
                self._active_extraction_progress_dialog = None
                self._active_extraction_progress_label = None
                self._active_extraction_cancel_button = None

    def _show_unsuccessful_extraction(
        self,
        state: ExtractionJobState,
        formatted_elapsed: str,
    ) -> None:
        if state.status is ExtractionJobStatus.CANCELLED:
            title = "Cancelled"
            message = f"Extraction was cancelled after {formatted_elapsed}."
        elif (
            state.status is ExtractionJobStatus.FAILED
            and (
                state.failure is ExtractionJobFailure.OUTPUT_MISSING
                or (
                    state.diagnostic is not None
                    and state.diagnostic.code
                    is ExtractionJobDiagnosticCode.OUTPUT_MISSING
                )
            )
        ):
            title = "Extraction failed"
            message = (
                "Extraction completed, but the output workbook was not found "
                f"after {formatted_elapsed}."
            )
        elif (
            state.status is ExtractionJobStatus.FAILED
            and state.diagnostic is not None
            and state.diagnostic.code is ExtractionJobDiagnosticCode.PROCESS_CRASH
        ):
            title = "Extraction process stopped unexpectedly"
            message = (
                "The extraction process stopped unexpectedly after "
                f"{formatted_elapsed}."
            )
        else:
            title = "Extraction failed"
            message = f"Extraction failed after {formatted_elapsed}."
        if state.diagnostic is not None:
            message = f"{message}\n\n{state.diagnostic.message}"
        QtWidgets.QMessageBox.information(self, title, message)

    def on_cancel_extraction(
            self,
            progress_label: QtWidgets.QLabel,
            total_label: QtWidgets.QLabel,
            recent_label: ElidedSelectableLabel,
            bar: QtWidgets.QProgressBar,
            cancel_btn: QtWidgets.QPushButton,
    ):
        if self._shutdown_state is not _WindowShutdownState.OPEN:
            return
        if not self.extraction_run.state.active:
            return

        # Prevent repeated clicking/prompting
        if self.extraction_run.state.status is ExtractionJobStatus.CANCELLING:
            return

        if QtWidgets.QMessageBox.question(cancel_btn, "Cancel extraction",
                                          "Stop the extraction now?") == QtWidgets.QMessageBox.Yes:
            if not self.extraction_run.request_cancel():
                return
            self._sync_pdf_extraction_enabled()
            _render_extraction_progress(
                self.extraction_run.state,
                progress_label,
                total_label,
                recent_label,
                bar,
            )
            cancel_btn.setEnabled(False)

    def _begin_active_extraction_shutdown(self) -> None:
        self._shutdown_state = _WindowShutdownState.STOPPING_EXTRACTION
        self.set_visible_title("Xtractor — Stopping extraction…")
        self.setWindowTitle(f"{self.windowTitle()} — Stopping extraction…")
        if hasattr(self, "extract_button"):
            self.extract_button.setText("Stopping extraction…")
            self.extract_button.setEnabled(False)
        if self._active_extraction_progress_dialog is not None:
            self._active_extraction_progress_dialog.setWindowTitle(
                "Stopping extraction…"
            )
        if self._active_extraction_progress_label is not None:
            self._active_extraction_progress_label.setText(
                "Stopping extraction…"
            )
        if self._active_extraction_cancel_button is not None:
            self._active_extraction_cancel_button.setEnabled(False)

        thread = _ExtractionShutdownThread(self.extraction_run, self)
        self._shutdown_thread = thread
        thread.finished.connect(self._on_extraction_shutdown_thread_finished)
        thread.start()

    @QtCore.Slot()
    def _on_extraction_shutdown_thread_finished(self) -> None:
        thread = self._shutdown_thread
        if thread is None:
            return
        if not thread.wait(0):
            QtCore.QTimer.singleShot(
                10,
                self._on_extraction_shutdown_thread_finished,
            )
            return

        self._shutdown_thread = None
        thread.deleteLater()
        self._shutdown_state = _WindowShutdownState.READY_TO_CLOSE
        if self._active_extraction_progress_dialog is not None:
            self._active_extraction_progress_dialog.accept()
        self.close()

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        if self._shutdown_state is _WindowShutdownState.STOPPING_EXTRACTION:
            event.ignore()
            return
        if self.extraction_run.state.active:
            event.ignore()
            self._begin_active_extraction_shutdown()
            return

        self._shutdown_state = _WindowShutdownState.READY_TO_CLOSE
        self.extraction_run.dispose()
        self.output_readiness.close()
        self._output_probe_timer.stop()
        self.pdf_selection_panel.shutdown()
        self.pdf_viewer.close_pdf()
        self._output_probe_thread_pool.clear()
        super().closeEvent(event)
