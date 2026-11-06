# main.py
# ────────────────────────────────────────────────────────────
# Xtractor launcher with splash screen & warm-up (PySide6)
# ────────────────────────────────────────────────────────────
import multiprocessing
import threading
import sys
from pathlib import Path

from app.logging_setup import configure_logging, log_file_path
from app.common.packaging import is_packaged_application, packaged_root

logger = configure_logging()
logger.info("App starting…")


def resource_path(rel: str) -> str:
    candidate = packaged_root() / rel
    if is_packaged_application() or candidate.exists():
        return str(candidate)
    # Source checkouts keep the style tree under app/ui; frozen builders map it
    # to the top-level ``style`` destination handled above.
    return str(Path(__file__).resolve().parent / "app" / "ui" / rel)


def _ci_find(dir_path: Path, name: str) -> str | None:
    if not dir_path.exists():
        return None
    low = name.lower()
    for p in dir_path.iterdir():
        if p.name.lower() == low:
            return str(p)
    return None


def asset(rel_name: str) -> str:
    base = Path(resource_path(""))
    p = base / "style" / rel_name
    if p.exists():
        return str(p)
    hit = _ci_find(base / "style", rel_name)
    if hit:
        return hit
    p = base / "app" / "ui" / "style" / rel_name
    if p.exists():
        return str(p)
    hit = _ci_find(base / "app" / "ui" / "style", rel_name)
    if hit:
        return hit
    return str(base / "style" / rel_name)


def _set_window_icon(window, icon_path: str) -> None:
    from PySide6 import QtGui

    try:
        window.setWindowIcon(QtGui.QIcon(icon_path))
    except Exception as exc:
        logger.debug(
            "Could not set window icon (%s)",
            type(exc).__name__,
        )


def warm_up(event, errors):
    try:
        logger.debug("Warm-up: importing heavy dependencies")
        from app.infra.pdf import initialize_pdf_backend

        initialize_pdf_backend()
        import openpyxl  # noqa: F401
        import PIL.Image  # noqa: F401
    except Exception as exc:  # pragma: no cover
        error_type = type(exc).__name__
        errors.append(error_type)
        logger.error("Warm-up failed while importing libraries (%s)", error_type)
    else:
        logger.debug("Warm-up completed successfully")
    finally:
        event.set()


def main():
    from PySide6 import QtCore, QtGui, QtWidgets

    from app.controllers.extraction_resource_admission import (
        ExtractionResourceAdmission,
    )
    from app.ui.gui import XtractorGUI
    from app.ui.theme import load_theme, set_theme
    from app.ui.constants import VERSION_TEXT

    if QtCore.QVersionNumber.fromString(QtCore.qVersion()).majorVersion() < 6:
        QtCore.QCoreApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling)
        QtCore.QCoreApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps)

    app = QtWidgets.QApplication(sys.argv)

    theme = load_theme(
        asset("xtractor.qss"),
        asset("xtractor.qss.json"),
        asset("xtractor-dark-red.json"),
    )
    set_theme(theme)
    if theme.stylesheet:
        app.setStyleSheet(theme.stylesheet)

    splash_pixmap = QtGui.QPixmap(asset("xtractor-logo.png")).scaledToHeight(
        320,
        QtCore.Qt.SmoothTransformation,
    )
    splash = QtWidgets.QSplashScreen(splash_pixmap)
    splash.setWindowOpacity(0.75)
    splash.showMessage("loading…", QtCore.Qt.AlignBottom | QtCore.Qt.AlignHCenter, QtGui.QColor("white"))
    splash.show()

    ready = threading.Event()
    warmup_errors = []
    threading.Thread(target=warm_up, args=(ready, warmup_errors), daemon=True).start()

    def finish():
        if warmup_errors:
            log_file = log_file_path()
            message = (
                "Xtractor could not load required libraries and needs to close.\n\n"
                f"Details: {warmup_errors[0]}\n\n"
                f"Log file (if available):\n{log_file}"
            )
            QtWidgets.QMessageBox.critical(None, "Xtractor startup error", message)
            app.quit()
            return
        extraction_admission = ExtractionResourceAdmission()
        window = XtractorGUI(extraction_admission=extraction_admission)
        window.setWindowTitle(f"Xtractor {VERSION_TEXT}")
        _set_window_icon(window, asset("xtractor-logo.png"))
        window.show()
        splash.finish(window)

    def check_ready():
        if ready.is_set():
            finish()
        else:
            QtCore.QTimer.singleShot(120, check_ready)

    QtCore.QTimer.singleShot(100, check_ready)
    sys.exit(app.exec())


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
