from __future__ import annotations

import logging
import multiprocessing
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.infra.app_data_path import app_data_dir

APP_NAME = "xtractor"
LOG_FILE_HANDLER_NAME = "xtractor_file"
LOG_UNAVAILABLE_HANDLER_NAME = "xtractor_unavailable"
LOG_CHILD_HANDLER_NAME = "xtractor_child_null"
LOG_MAX_BYTES = 2_000_000
LOG_BACKUP_COUNT = 3


class _ApplicationLogFilter(logging.Filter):
    """Keep persisted records inside the reviewed application logging surface."""

    def filter(self, record: logging.LogRecord) -> bool:
        is_application_logger = (
            record.name in {"app", APP_NAME} or record.name.startswith("app.")
        )
        if not is_application_logger:
            return False
        # Exception messages and tracebacks can contain file paths or document data.
        record.exc_info = None
        record.exc_text = None
        return True


class _MainProcessRotatingFileHandler(RotatingFileHandler):
    """A bounded file sink that never lets spawned/forked workers write to it."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._owner_pid = os.getpid()

    def emit(self, record: logging.LogRecord) -> None:
        if os.getpid() != self._owner_pid:
            return
        super().emit(record)

    def handleError(self, record: logging.LogRecord) -> None:
        # The stdlib handler's fallback prints exception details to stderr.
        # Logging failures must not expose record data or prevent UI startup.
        return


def _log_dir() -> Path:
    override = os.getenv("XTRACTOR_LOG_DIR")
    return Path(override) if override else app_data_dir() / "Logs"


def log_file_path() -> Path:
    """Return the configured log path without creating directories or files."""
    return _log_dir() / "app.log"


def _handler_named(root: logging.Logger, name: str) -> logging.Handler | None:
    return next(
        (
            handler
            for handler in root.handlers
            if getattr(handler, "name", None) == name
        ),
        None,
    )


def _add_null_handler(root: logging.Logger, name: str) -> None:
    if _handler_named(root, name) is None:
        handler = logging.NullHandler()
        handler.name = name
        root.addHandler(handler)


def _configure_child_logging(root: logging.Logger) -> None:
    # Spawned children may re-import GUI startup code; forked children may
    # inherit the parent's open file descriptor. Neither should write or rotate.
    for handler in tuple(root.handlers):
        if getattr(handler, "name", None) == LOG_FILE_HANDLER_NAME:
            root.removeHandler(handler)
            handler.close()
    _add_null_handler(root, LOG_CHILD_HANDLER_NAME)


def configure_logging(level: int | None = None) -> logging.Logger:
    level_name = (os.getenv("XTRACTOR_LOG_LEVEL") or "").upper()
    env_level = getattr(logging, level_name, None) if level_name else None
    resolved_level = env_level if isinstance(env_level, int) else level
    if resolved_level is None:
        resolved_level = logging.INFO

    root = logging.getLogger()
    root.setLevel(resolved_level)
    if multiprocessing.current_process().name != "MainProcess":
        _configure_child_logging(root)
        return logging.getLogger(APP_NAME)

    if _handler_named(root, LOG_FILE_HANDLER_NAME) is not None:
        return logging.getLogger(APP_NAME)

    try:
        path = log_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = _MainProcessRotatingFileHandler(
            path,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
    except Exception:
        # No console fallback: it could surface the same unreviewed payloads.
        _add_null_handler(root, LOG_UNAVAILABLE_HANDLER_NAME)
        return logging.getLogger(APP_NAME)

    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)s | %(processName)s | "
            "%(name)s:%(lineno)d | %(message)s"
        )
    )
    handler.addFilter(_ApplicationLogFilter())
    handler.name = LOG_FILE_HANDLER_NAME
    root.addHandler(handler)

    unavailable = _handler_named(root, LOG_UNAVAILABLE_HANDLER_NAME)
    if unavailable is not None:
        root.removeHandler(unavailable)
        unavailable.close()

    return logging.getLogger(APP_NAME)
