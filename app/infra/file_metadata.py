"""Streaming source-file metadata and SHA-256 fingerprint helpers."""

from __future__ import annotations

import errno
import hashlib
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping


HASH_CHUNK_SIZE = 1024 * 1024
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FileFingerprint:
    sha256: str = ""
    size_bytes: int = 0
    modified_time_ns: int = 0
    modified_display: str = ""
    source_changed_during_fingerprint: bool = False
    warning: str = ""
    stat_available: bool = False

    def __post_init__(self) -> None:
        if self.sha256:
            object.__setattr__(self, "sha256", self.sha256.lower())

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, object] | None) -> "FileFingerprint":
        payload = payload or {}
        return cls(
            sha256=str(payload.get("sha256") or ""),
            size_bytes=_as_int(payload.get("size_bytes")),
            modified_time_ns=_as_int(payload.get("modified_time_ns")),
            modified_display=str(payload.get("modified_display") or ""),
            source_changed_during_fingerprint=bool(
                payload.get("source_changed_during_fingerprint", False)
            ),
            warning=str(payload.get("warning") or ""),
            stat_available=bool(payload.get("stat_available", False)),
        )


def sha256_file(path: Path, *, chunk_size: int = HASH_CHUNK_SIZE) -> str:
    """Hash a file with bounded memory usage."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    hasher = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def fingerprint_file(
    path: Path,
    *,
    hash_function: Callable[[Path], str] | None = None,
    stat_function: Callable[[Path], os.stat_result] | None = None,
    max_retries: int = 1,
) -> FileFingerprint:
    """Capture metadata and hash bytes, retrying once for a changing source."""
    path = Path(path)
    hash_function = hash_function or sha256_file
    stat_function = stat_function or Path.stat
    try:
        before = _snapshot(stat_function(path))
    except Exception as exc:
        _log_metadata_failure("Source metadata", exc)
        return FileFingerprint(
            warning=format_metadata_warning("Source metadata unavailable", exc)
        )

    last_before = before
    changed_seen = False
    for attempt in range(max(0, max_retries) + 1):
        try:
            digest = str(hash_function(path)).lower()
        except Exception as exc:
            _log_metadata_failure("SHA256", exc)
            return _fingerprint_from_snapshot(
                last_before,
                warning=format_metadata_warning("SHA256 unavailable", exc),
            )
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            return _fingerprint_from_snapshot(
                last_before,
                warning="SHA256 unavailable: hash helper returned an invalid digest",
            )
        try:
            after = _snapshot(stat_function(path))
        except Exception as exc:
            _log_metadata_failure("Source metadata after hashing", exc)
            return _fingerprint_from_snapshot(
                last_before,
                sha256=digest,
                warning=format_metadata_warning(
                    "Source metadata unavailable after hashing",
                    exc,
                ),
            )
        if after[:2] == last_before[:2]:
            return _fingerprint_from_snapshot(
                after,
                sha256=digest,
                changed=changed_seen,
                warning=(
                    "Source file changed while hashing; fingerprint retried"
                    if changed_seen
                    else ""
                ),
            )
        changed_seen = True
        if attempt >= max_retries:
            return _fingerprint_from_snapshot(
                after,
                sha256=digest,
                changed=True,
                warning="Source file changed while hashing",
            )
        last_before = after

    return _fingerprint_from_snapshot(
        last_before,
        warning="Source file could not be fingerprinted",
    )


def source_changed_since(path: Path, fingerprint: FileFingerprint) -> tuple[bool, str]:
    """Compare size and mtime_ns after extraction without rehashing."""
    if not fingerprint.stat_available:
        return False, ""
    try:
        current = _snapshot(Path(path).stat())
    except Exception as exc:
        _log_metadata_failure("Source metadata after extraction", exc)
        return True, format_metadata_warning(
            "Source metadata unavailable after extraction",
            exc,
        )
    if current[:2] != (fingerprint.size_bytes, fingerprint.modified_time_ns):
        return True, "Source file changed during extraction"
    return False, ""


def _snapshot(stat_result: os.stat_result) -> tuple[int, int, str]:
    modified_time_ns = getattr(stat_result, "st_mtime_ns", None)
    if modified_time_ns is None:
        modified_time_ns = int(float(stat_result.st_mtime) * 1_000_000_000)
    display = datetime.fromtimestamp(float(stat_result.st_mtime)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    return int(stat_result.st_size), int(modified_time_ns), display


def _fingerprint_from_snapshot(
    snapshot: tuple[int, int, str],
    *,
    sha256: str = "",
    changed: bool = False,
    warning: str = "",
) -> FileFingerprint:
    return FileFingerprint(
        sha256=sha256 if len(sha256) == 64 else sha256,
        size_bytes=snapshot[0],
        modified_time_ns=snapshot[1],
        modified_display=snapshot[2],
        source_changed_during_fingerprint=changed,
        warning=warning,
        stat_available=True,
    )


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _log_metadata_failure(operation: str, exception: Exception) -> None:
    exception_type = type(exception).__name__
    logger.debug("%s failed (%s)", operation, exception_type)


def format_metadata_warning(operation: str, exception: Exception) -> str:
    """Format a safe, useful warning for structured user-facing metadata output."""
    exception_type = type(exception).__name__
    if isinstance(exception, OSError):
        cause = _safe_os_cause(exception)
    else:
        cause = "unexpected metadata failure"
    return f"{operation}: {cause} ({exception_type})"


def _safe_os_cause(exception: OSError) -> str:
    if isinstance(exception, PermissionError):
        return "permission denied"
    if isinstance(exception, FileNotFoundError):
        return "file not found"
    if isinstance(exception, (NotADirectoryError, IsADirectoryError)):
        return "path unavailable"

    causes = {
        errno.EACCES: "permission denied",
        errno.EPERM: "operation not permitted",
        errno.ENOENT: "file not found",
        errno.ENOTDIR: "path unavailable",
        errno.ENODEV: "device unavailable",
        errno.ENXIO: "device unavailable",
        errno.EIO: "device unavailable",
        errno.EBUSY: "resource busy",
    }
    return causes.get(getattr(exception, "errno", None), "operating-system error")
