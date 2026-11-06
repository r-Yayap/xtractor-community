from __future__ import annotations

import os
import sys
from pathlib import Path


def app_data_dir() -> Path:
    """Return Xtractor's per-user application data directory without creating it."""
    if sys.platform == "win32":
        base = Path(os.getenv("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        return base / "Xtractor"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Xtractor"
    base = Path(os.getenv("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / "xtractor"
