from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from app.infra.ocr.errors import OcrSubsystemError
from app.infra.ocr.models import OcrDiagnostic, OcrDiagnosticCode


@dataclass(frozen=True, slots=True)
class OcrRuntime:
    executable: Path
    tessdata_dir: Path
    version: str = "local-installation"


def _find_executable() -> Path | None:
    configured = os.getenv("XTRACTOR_TESSERACT_CMD")
    if configured:
        return Path(configured).expanduser()

    for variable, parts in (
        ("ProgramFiles", ("Tesseract-OCR", "tesseract.exe")),
        ("ProgramFiles(x86)", ("Tesseract-OCR", "tesseract.exe")),
        ("LOCALAPPDATA", ("Programs", "Tesseract-OCR", "tesseract.exe")),
    ):
        directory = os.getenv(variable)
        if directory:
            executable = Path(directory).joinpath(*parts)
            if executable.is_file():
                return executable

    discovered = shutil.which("tesseract.exe") or shutil.which("tesseract")
    return Path(discovered) if discovered else None


def discover_runtime(tessdata_dir: Path | None = None) -> OcrRuntime:
    """Resolve a local Tesseract executable and its English language data."""
    executable = _find_executable()
    if executable is None or not executable.is_file():
        raise OcrSubsystemError(
            OcrDiagnostic(
                OcrDiagnosticCode.EXECUTABLE_UNAVAILABLE,
                "Tesseract was unavailable",
            )
        )
    executable = executable.resolve()

    configured_data = os.getenv("XTRACTOR_TESSDATA_DIR")
    if tessdata_dir is not None:
        data = Path(tessdata_dir).expanduser()
    elif configured_data:
        data = Path(configured_data).expanduser()
    else:
        data = executable.parent / "tessdata"
    if not (data / "eng.traineddata").is_file():
        raise OcrSubsystemError(
            OcrDiagnostic(
                OcrDiagnosticCode.TESSDATA_UNAVAILABLE,
                "Tesseract English language data was unavailable",
            )
        )

    return OcrRuntime(executable, data.resolve())
