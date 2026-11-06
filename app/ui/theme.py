from __future__ import annotations

import json
import platform
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from PySide6 import QtGui, QtWidgets
from app.common.packaging import is_packaged_application, packaged_root

_THEME: "Theme | None" = None


@dataclass(frozen=True)
class Theme:
    tokens: dict[str, Any]
    stylesheet: str


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(base.get(key), dict):
            base[key] = _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _ctk_color(theme: Mapping[str, Any], section: str, key: str, fallback: str) -> str:
    try:
        value = theme[section][key]
    except KeyError:
        return fallback
    if isinstance(value, list):
        return value[-1]
    return value


def _ctk_font(theme: Mapping[str, Any]) -> dict[str, Any]:
    fonts = theme.get("CTkFont", {})
    if not isinstance(fonts, Mapping):
        return {"family": "Roboto", "size": 13, "weight": "normal"}
    system = platform.system()
    key = {
        "Darwin": "macOS",
        "Windows": "Windows",
        "Linux": "Linux",
    }.get(system, "Windows")
    return fonts.get(key, next(iter(fonts.values()), {"family": "Roboto", "size": 13, "weight": "normal"}))


def _resource_base_dir() -> Path:
    base = packaged_root()
    if is_packaged_application() or (base / "style").is_dir():
        return base
    return Path(__file__).resolve().parent


def _base_tokens(ctk_theme: Mapping[str, Any]) -> dict[str, Any]:
    accent = _ctk_color(ctk_theme, "CTkButton", "fg_color", "#C10206")
    accent_hover = _ctk_color(ctk_theme, "CTkButton", "hover_color", "#4b4b4b")
    bg = _ctk_color(ctk_theme, "CTk", "fg_color", "#212121")
    surface = _ctk_color(ctk_theme, "CTkFrame", "fg_color", "#212121")
    surface_alt = _ctk_color(ctk_theme, "CTkEntry", "fg_color", "#000000")
    text = _ctk_color(ctk_theme, "CTkLabel", "text_color", "#f5f5f5")
    text_muted = _ctk_color(ctk_theme, "CTkEntry", "placeholder_text_color", "#4b4b4b")
    border = _ctk_color(ctk_theme, "CTkFrame", "border_color", "#4b4b4b")
    canvas = _ctk_color(ctk_theme, "CTkTextbox", "fg_color", "#1e1e1e")

    font = _ctk_font(ctk_theme)

    base_dir = _resource_base_dir()
    icons_dir = base_dir / "style" / "icons"

    return {
        "colors": {
            "bg": bg,
            "surface": surface,
            "surface_alt": surface_alt,
            "surface_soft": "#252525",
            "canvas": canvas,
            "text": text,
            "text_muted": text_muted,
            "border": border,
            "accent": accent,
            "accent_hover": accent_hover,
            "accent_soft": "#ff5f5f",
            "tab_active": accent,
            "tab_inactive": surface_alt,
            "mode_area": "#3949AB",
            "mode_resize": "#EF6C00",
            "selection": accent,
            "warning": "#ffb74d",
            "success": "#69d391",
            "success_surface": "#183d2a",
            "warning_surface": "#4a3515",
            "danger": "#ff7b82",
            "danger_surface": "#2d2022",
            "information": "#8ab4f8",
            "information_surface": "#1f334f",
            "pill_identity_surface": "rgba(43, 43, 43, 180)",
            "pill_status_surface": "rgba(43, 43, 43, 125)",
            "error_surface": "#5a2024",
        },
        "spacing": {
            "xs": 4,
            "sm": 6,
            "md": 8,
            "lg": 12,
            "xl": 16,
            "xxl": 20,
        },
        "font": {
            "family": font.get("family", "Roboto"),
            "size_base": font.get("size", 13),
            "size_sm": max(11, font.get("size", 13) - 1),
            "size_lg": font.get("size", 13) + 1,
            "size_xl": font.get("size", 13) + 3,
            "weight_normal": 400,
            "weight_semibold": 600,
        },
        "radii": {
            "sm": 6,
            "md": 10,
            "lg": 14,
            "pill": 18,
        },
        "sizes": {
            "button_height": 30,
            "input_height": 28,
            "tab_height": 36,
            "scrollbar": 12,
            "table_row": 22,
            "table_header": 18,
            "coordinates_table_min_height": 240,
        },
        "shadow": {
            "blur": 20,
            "offset_x": 0,
            "offset_y": 6,
            "color": "rgba(0, 0, 0, 120)",
        },
        "paths": {
            "icons": icons_dir.as_posix(),
        },
    }


def _resolve_token(tokens: Mapping[str, Any], section: str, key: str) -> str:
    try:
        value = tokens[section][key]
    except Exception:
        return f"{{{section}[{key}]}}"
    return str(value)


def render_qss(template: str, tokens: Mapping[str, Any]) -> str:
    pattern = re.compile(r"\{([a-zA-Z0-9_]+)\[([^\]]+)\]\}")

    def replace(match: re.Match[str]) -> str:
        section, key = match.group(1), match.group(2)
        return _resolve_token(tokens, section, key)

    return pattern.sub(replace, template)


def load_theme(qss_path: str | Path, tokens_path: str | Path, palette_path: str | Path) -> Theme:
    qss_path = Path(qss_path)
    tokens_path = Path(tokens_path)
    palette_path = Path(palette_path)

    ctk_theme = _load_json(palette_path)
    tokens = _base_tokens(ctk_theme)
    overrides = _load_json(tokens_path)
    if overrides:
        tokens = _deep_merge(tokens, overrides)

    qss_template = qss_path.read_text(encoding="utf-8")
    stylesheet = render_qss(qss_template, tokens)

    return Theme(tokens=tokens, stylesheet=stylesheet)


def set_theme(theme: Theme) -> None:
    global _THEME
    _THEME = theme


def get_theme() -> Theme:
    if _THEME is None:
        return Theme(tokens=_base_tokens({}), stylesheet="")
    return _THEME


def apply_shadow(widget: QtWidgets.QWidget, tokens: Mapping[str, Any] | None = None) -> None:
    if tokens is None:
        tokens = get_theme().tokens
    shadow_tokens = tokens.get("shadow", {})
    effect = QtWidgets.QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(float(shadow_tokens.get("blur", 20)))
    effect.setOffset(
        float(shadow_tokens.get("offset_x", 0)),
        float(shadow_tokens.get("offset_y", 6)),
    )
    effect.setColor(QtGui.QColor(shadow_tokens.get("color", "rgba(0, 0, 0, 120)")))
    widget.setGraphicsEffect(effect)
