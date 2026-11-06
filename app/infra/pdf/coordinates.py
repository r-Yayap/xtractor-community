from __future__ import annotations

import math

from app.infra.pdf.models import PdfRect


def _validate_zoom(zoom: float) -> float:
    value = float(zoom)
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError("zoom must be a positive finite number")
    return value


def scene_rect_to_pdf(rect: PdfRect, zoom: float) -> PdfRect:
    scale = _validate_zoom(zoom)
    return PdfRect(*(value / scale for value in rect.to_tuple()))


def pdf_rect_to_scene(rect: PdfRect, zoom: float) -> PdfRect:
    scale = _validate_zoom(zoom)
    return PdfRect(*(value * scale for value in rect.to_tuple()))


def adjust_rect_for_rotation(
    rect: PdfRect,
    *,
    rotation_degrees: int,
    page_height: float,
    page_width: float,
) -> PdfRect:
    x0, y0, x1, y1 = rect.to_tuple()
    if rotation_degrees == 0:
        return rect
    if rotation_degrees == 90:
        return PdfRect(y0, page_width - x1, y1, page_width - x0)
    if rotation_degrees == 180:
        return PdfRect(
            page_width - x1,
            page_height - y1,
            page_width - x0,
            page_height - y0,
        )
    if rotation_degrees == 270:
        return PdfRect(page_height - y1, x0, page_height - y0, x1)
    raise ValueError("rotation must be 0, 90, 180, or 270 degrees")


def sanitize_clip(
    rect: PdfRect,
    page_bounds: PdfRect,
    *,
    minimum_size: float = 1e-3,
) -> PdfRect | None:
    if not rect.is_finite or not page_bounds.is_finite:
        return None
    effective = rect.normalized().intersect(page_bounds.normalized())
    if effective is None:
        return None
    if effective.width <= minimum_size or effective.height <= minimum_size:
        return None
    return effective
