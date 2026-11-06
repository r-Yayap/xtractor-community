from __future__ import annotations

from pathlib import Path
import sys


def is_packaged_application() -> bool:
    return bool(getattr(sys, "frozen", False) or globals().get("__compiled__"))


def packaged_root() -> Path:
    # In source mode resources live at the repository root (the parent of the
    # ``app`` package), not beside the developer's Python interpreter.  Frozen
    # builders keep their data next to the executable (Nuitka) or expose it via
    # ``_MEIPASS`` (PyInstaller).
    if not is_packaged_application():
        return Path(__file__).resolve().parents[2]
    executable_root = Path(sys.executable).resolve().parent
    pyinstaller_root = getattr(sys, "_MEIPASS", None)
    if pyinstaller_root:
        return Path(pyinstaller_root).resolve()
    compiled = globals().get("__compiled__")
    containing_dir = getattr(compiled, "containing_dir", None)
    return Path(containing_dir or executable_root).resolve()
