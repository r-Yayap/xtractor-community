from __future__ import annotations

import ctypes
from dataclasses import dataclass

import pypdfium2.raw as pdfium_c

from app.infra.pdf.models import PdfRect
from app.infra.pdf.text_normalization import normalize_pdf_text


_CONFIDENT_COVERAGE = 0.72
_POSSIBLE_COVERAGE = 0.30
_OPAQUE_ALPHA = 242
_PAINT_SCAN_OBJECT_LIMIT = 100_000


@dataclass(frozen=True, slots=True)
class LayeredCell:
    primary: str
    display: str
    has_visible_layer: bool


@dataclass(frozen=True, slots=True)
class _PageObject:
    sequence: int
    kind: int
    rect: PdfRect
    text: str = ""
    fill_alpha: int | None = None
    filled: bool = False
    visibility_hint: str | None = None


def extract_layered_regions(
    page,
    space,
    rects,
    *,
    inspect_paint_order: bool | None = None,
) -> tuple[LayeredCell, ...]:
    if not rects:
        return ()
    region = _union_rect(tuple(rects))
    objects = _layer_objects_in_region(
        page,
        space,
        region,
        inspect_paint_order=inspect_paint_order,
    )
    text_objects = tuple(
        item for item in objects if item.kind == pdfium_c.FPDF_PAGEOBJ_TEXT and item.text
    )
    output: list[LayeredCell] = []
    for rect in rects:
        assigned = []
        for item in text_objects:
            status = _visibility_status(item, objects)
            center_x = (item.rect.x0 + item.rect.x1) / 2
            center_y = (item.rect.y0 + item.rect.y1) / 2
            center_inside = (
                rect.x0 <= center_x <= rect.x1
                and rect.y0 <= center_y <= rect.y1
            )
            if not center_inside and (
                status is None or item.rect.intersect(rect) is None
            ):
                continue
            assigned.append((item, status))
        output.append(_make_cell(assigned))
    return tuple(output)


def _layer_objects_in_region(
    page,
    space,
    region: PdfRect,
    *,
    inspect_paint_order: bool | None,
) -> tuple[_PageObject, ...]:
    try:
        text_objects = _text_objects_in_region(page, space, region)
    except Exception:
        return _objects_in_region(page, space, region)
    has_clipped_text = any(
        item.visibility_hint is not None for item in text_objects
    )
    if inspect_paint_order is None:
        try:
            if pdfium_c.FPDFPage_CountObjects(page) > _PAINT_SCAN_OBJECT_LIMIT:
                inspect_paint_order = False
        except Exception:
            pass
    should_inspect_paint = (
        False
        if inspect_paint_order is False
        else (
            inspect_paint_order is True
            or _has_strong_unlabelled_overlap(text_objects)
            or not has_clipped_text
        )
    )
    if should_inspect_paint:
        return _objects_in_region(page, space, region)
    return text_objects


def _text_objects_in_region(page, space, region: PdfRect) -> tuple[_PageObject, ...]:
    text_page = page.get_textpage()
    try:
        groups: dict[int, dict[str, object]] = {}
        skipped: set[int] = set()
        for character_index in range(text_page.count_chars()):
            raw_object = pdfium_c.FPDFText_GetTextObject(
                text_page,
                character_index,
            )
            if not raw_object:
                continue
            identity = ctypes.cast(raw_object, ctypes.c_void_p).value
            if identity is None or identity in skipped:
                continue
            group = groups.get(identity)
            if group is None:
                raw_rect = _raw_object_bounds(raw_object)
                rect = space.pdfium_to_app(raw_rect)
                if rect.intersect(region) is None:
                    skipped.add(identity)
                    continue
                group = {
                    "sequence": character_index,
                    "rect": rect,
                    "characters": [],
                    "status": _clip_visibility_status(
                        raw_object,
                        raw_rect,
                    ),
                }
                groups[identity] = group
            value = text_page.get_text_range(
                character_index,
                1,
                errors="replace",
            )
            if value:
                group["characters"].append(value)
        return tuple(
            _PageObject(
                sequence=int(group["sequence"]),
                kind=pdfium_c.FPDF_PAGEOBJ_TEXT,
                rect=group["rect"],
                text=normalize_pdf_text("".join(group["characters"])),
                visibility_hint=group["status"],
            )
            for group in groups.values()
            if group["characters"]
            and normalize_pdf_text("".join(group["characters"]))
        )
    finally:
        text_page.close()


def _raw_object_bounds(raw_object) -> PdfRect:
    left = ctypes.c_float()
    bottom = ctypes.c_float()
    right = ctypes.c_float()
    top = ctypes.c_float()
    if not pdfium_c.FPDFPageObj_GetBounds(
        raw_object,
        ctypes.byref(left),
        ctypes.byref(bottom),
        ctypes.byref(right),
        ctypes.byref(top),
    ):
        raise RuntimeError("Could not read PDFium text-object bounds.")
    return PdfRect(left.value, bottom.value, right.value, top.value)


def _has_strong_unlabelled_overlap(
    objects: tuple[_PageObject, ...],
) -> bool:
    visible = tuple(
        item
        for item in objects
        if item.kind == pdfium_c.FPDF_PAGEOBJ_TEXT
        and item.text
        and item.visibility_hint is None
    )
    for index, left in enumerate(visible):
        for right in visible[index + 1 :]:
            if (
                left.text == right.text
                and left.rect.to_tuple() == right.rect.to_tuple()
            ):
                continue
            intersection = left.rect.intersect(right.rect)
            if intersection is None:
                continue
            smaller = min(
                left.rect.width * left.rect.height,
                right.rect.width * right.rect.height,
            )
            if smaller <= 0:
                continue
            overlap = intersection.width * intersection.height / smaller
            if overlap >= 0.65:
                return True
    return False


def _objects_in_region(page, space, region: PdfRect) -> tuple[_PageObject, ...]:
    output: list[_PageObject] = []
    text_page = page.get_textpage()
    try:
        for sequence, obj in enumerate(page.get_objects(max_depth=15, textpage=text_page)):
            try:
                raw_rect = PdfRect.from_value(obj.get_bounds())
                rect = space.pdfium_to_app(raw_rect)
            except Exception:
                continue
            if rect.intersect(region) is None:
                continue
            text = ""
            if obj.type == pdfium_c.FPDF_PAGEOBJ_TEXT:
                try:
                    text = normalize_pdf_text(obj.extract())
                except Exception:
                    text = ""
            fill_alpha, filled = _path_fill(obj)
            visibility_hint = (
                _clip_visibility_status(obj, raw_rect)
                if obj.type == pdfium_c.FPDF_PAGEOBJ_TEXT
                else None
            )
            output.append(
                _PageObject(
                    sequence=sequence,
                    kind=obj.type,
                    rect=rect,
                    text=text,
                    fill_alpha=fill_alpha,
                    filled=filled,
                    visibility_hint=visibility_hint,
                )
            )
    finally:
        text_page.close()
    return tuple(output)


def _path_fill(obj) -> tuple[int | None, bool]:
    if obj.type != pdfium_c.FPDF_PAGEOBJ_PATH:
        return None, False
    fill_mode = ctypes.c_int()
    stroked = ctypes.c_int()
    if not pdfium_c.FPDFPath_GetDrawMode(
        obj, ctypes.byref(fill_mode), ctypes.byref(stroked)
    ):
        return None, False
    if fill_mode.value == pdfium_c.FPDF_FILLMODE_NONE:
        return None, False
    channels = [ctypes.c_uint() for _ in range(4)]
    if not pdfium_c.FPDFPageObj_GetFillColor(
        obj, *(ctypes.byref(channel) for channel in channels)
    ):
        return None, True
    return channels[3].value, True


def _visibility_status(
    text: _PageObject,
    objects: tuple[_PageObject, ...],
) -> str | None:
    if text.visibility_hint is not None:
        return text.visibility_hint
    possible = False
    for later in objects:
        if later.sequence <= text.sequence:
            continue
        coverage = _coverage(text.rect, later.rect)
        if coverage < _POSSIBLE_COVERAGE:
            continue
        if later.kind == pdfium_c.FPDF_PAGEOBJ_PATH and later.filled:
            if later.fill_alpha is not None and later.fill_alpha >= _OPAQUE_ALPHA:
                if coverage >= _CONFIDENT_COVERAGE:
                    return "OCCLUDED"
            else:
                possible = True
        elif later.kind in (
            pdfium_c.FPDF_PAGEOBJ_IMAGE,
            pdfium_c.FPDF_PAGEOBJ_FORM,
        ):
            possible = True
    return "POSSIBLY OCCLUDED" if possible else None


def _clip_visibility_status(obj, text_rect: PdfRect) -> str | None:
    try:
        clip = pdfium_c.FPDFPageObj_GetClipPath(obj)
        path_count = pdfium_c.FPDFClipPath_CountPaths(clip) if clip else -1
    except Exception:
        return None
    if path_count <= 0:
        return None
    clip_bounds: PdfRect | None = None
    for path_index in range(path_count):
        try:
            segment_count = pdfium_c.FPDFClipPath_CountPathSegments(
                clip, path_index
            )
        except Exception:
            continue
        points: list[tuple[float, float]] = []
        for segment_index in range(max(0, segment_count)):
            segment = pdfium_c.FPDFClipPath_GetPathSegment(
                clip, path_index, segment_index
            )
            if not segment:
                continue
            x = ctypes.c_float()
            y = ctypes.c_float()
            if pdfium_c.FPDFPathSegment_GetPoint(
                segment, ctypes.byref(x), ctypes.byref(y)
            ):
                points.append((float(x.value), float(y.value)))
        if not points:
            continue
        path_bounds = PdfRect(
            min(point[0] for point in points),
            min(point[1] for point in points),
            max(point[0] for point in points),
            max(point[1] for point in points),
        )
        clip_bounds = (
            path_bounds
            if clip_bounds is None
            else clip_bounds.intersect(path_bounds)
        )
        if clip_bounds is None:
            return "CLIPPED"
    return (
        None
        if clip_bounds is None
        else _clip_bounds_status(text_rect, clip_bounds)
    )


def _clip_bounds_status(
    text_rect: PdfRect,
    clip_bounds: PdfRect,
) -> str | None:
    intersection = text_rect.intersect(clip_bounds)
    if intersection is None:
        return "CLIPPED"
    area = text_rect.width * text_rect.height
    if area <= 0:
        return None
    coverage = intersection.width * intersection.height / area
    return None if coverage >= 0.98 else "POSSIBLY CLIPPED"


def _make_cell(items: list[tuple[_PageObject, str | None]]) -> LayeredCell:
    if not items:
        return LayeredCell("", "", False)
    deduplicated: list[tuple[_PageObject, str | None]] = []
    seen: set[tuple[str, tuple[float, float, float, float], str | None]] = set()
    for item, status in items:
        key = (
            item.text,
            tuple(round(value, 3) for value in item.rect.to_tuple()),
            status,
        )
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append((item, status))
    items = deduplicated
    visible = [item for item, status in items if status is None]
    concealed = [(item, status) for item, status in items if status is not None]
    visible_text = _join_text_objects(visible)
    if visible_text:
        primary = visible_text
        lines = [visible_text]
        has_visible = True
    else:
        primary = _join_text_objects([item for item, _status in concealed])
        lines = []
        has_visible = False
    for item, status in concealed:
        lines.append(f"[{status}] {item.text}")
    return LayeredCell(primary, "\n".join(lines), has_visible)


def _join_text_objects(items: list[_PageObject]) -> str:
    if not items:
        return ""
    ordered = sorted(
        items,
        key=lambda item: (
            round((item.rect.y0 + item.rect.y1) / 2, 1),
            item.rect.x0,
            item.sequence,
        ),
    )
    lines: list[list[_PageObject]] = []
    for item in ordered:
        center = (item.rect.y0 + item.rect.y1) / 2
        if not lines:
            lines.append([item])
            continue
        previous_center = sum(
            (entry.rect.y0 + entry.rect.y1) / 2 for entry in lines[-1]
        ) / len(lines[-1])
        tolerance = max(item.rect.height, *(entry.rect.height for entry in lines[-1])) * 0.6
        if abs(center - previous_center) <= tolerance:
            lines[-1].append(item)
        else:
            lines.append([item])
    return "\n".join(
        " ".join(entry.text.strip() for entry in line if entry.text.strip())
        for line in lines
    ).strip()


def _coverage(target: PdfRect, cover: PdfRect) -> float:
    area = target.width * target.height
    return 0.0 if area <= 0 else _intersection_area(target, cover) / area


def _intersection_area(left: PdfRect, right: PdfRect) -> float:
    intersection = left.intersect(right)
    return 0.0 if intersection is None else intersection.width * intersection.height


def _union_rect(rects: tuple[PdfRect, ...]) -> PdfRect:
    return PdfRect(
        min(rect.x0 for rect in rects),
        min(rect.y0 for rect in rects),
        max(rect.x1 for rect in rects),
        max(rect.y1 for rect in rects),
    )
