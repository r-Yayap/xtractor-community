"""Immutable file-level extraction manifest entries."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Mapping


def is_valid_sha256(value: object) -> bool:
    """Return whether a value is an exact SHA-256 hexadecimal digest."""
    text = str(value or "").strip()
    return len(text) == 64 and all(
        character in "0123456789abcdefABCDEF" for character in text
    )


def has_authoritative_identity(entry: "FileManifestEntry") -> bool:
    """Return whether the entry's digest is safe for identity inference."""
    return (
        is_valid_sha256(entry.sha256)
        and not entry.source_changed_during_extraction
    )


def source_absolute_path(path: os.PathLike[str] | str) -> str:
    """Return a fully qualified source path, preserving qualified input."""

    raw = os.fspath(path)
    drive, _ = os.path.splitdrive(raw)
    if drive and os.path.isabs(raw):
        return raw
    return os.path.abspath(raw)


@dataclass(frozen=True, slots=True)
class FileManifestEntry:
    file_id: str
    sha256: str
    size_bytes: int
    modified_display: str
    modified_time_ns: int
    folder: str
    filename: str
    relative_path: str
    duration_ms: float
    status: str
    extraction_error: str = ""
    source_changed_during_extraction: bool = False
    content_group: str = ""
    current_relationship: str = ""
    related_files: tuple[str, ...] = ()
    warning_error: str = ""
    absolute_path: str = ""

    def __post_init__(self) -> None:
        if self.sha256:
            object.__setattr__(self, "sha256", str(self.sha256).strip().lower())

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["related_files"] = list(self.related_files)
        return result

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "FileManifestEntry":
        raw_related = payload.get("related_files", ())
        if isinstance(raw_related, str):
            related = (raw_related,)
        else:
            related = tuple(str(item) for item in (raw_related or ()))
        return cls(
            file_id=str(payload.get("file_id") or payload.get("prefix") or ""),
            sha256=str(payload.get("sha256") or ""),
            size_bytes=_as_int(payload.get("size_bytes")),
            modified_display=str(payload.get("modified_display") or ""),
            modified_time_ns=_as_int(payload.get("modified_time_ns")),
            folder=str(payload.get("folder") or ""),
            filename=str(payload.get("filename") or ""),
            relative_path=str(payload.get("relative_path") or ""),
            duration_ms=_as_float(payload.get("duration_ms")),
            status=str(payload.get("status") or "Unknown"),
            extraction_error=str(payload.get("extraction_error") or ""),
            source_changed_during_extraction=bool(
                payload.get("source_changed_during_extraction", False)
            ),
            content_group=str(payload.get("content_group") or ""),
            current_relationship=str(payload.get("current_relationship") or ""),
            related_files=related,
            warning_error=str(payload.get("warning_error") or ""),
            absolute_path=str(payload.get("absolute_path") or ""),
        )


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _as_float(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
