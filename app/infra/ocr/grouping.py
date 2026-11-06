from __future__ import annotations

import math
from dataclasses import dataclass

from app.infra.ocr.models import OcrRect, OcrWord


@dataclass(frozen=True, slots=True)
class OcrRegion:
    area_id: str
    rect: OcrRect

    def __post_init__(self) -> None:
        if not self.area_id:
            raise ValueError("OCR region ID is required")
        if self.rect.width <= 0 or self.rect.height <= 0:
            raise ValueError("OCR region rectangle must have positive area")


@dataclass(frozen=True, slots=True)
class OcrRegionGroup:
    regions: tuple[OcrRegion, ...]
    bounds: OcrRect
    pixel_width: int
    pixel_height: int

    @property
    def pixel_count(self) -> int:
        return self.pixel_width * self.pixel_height


@dataclass(frozen=True, slots=True)
class RegionAssignment:
    words_by_area: dict[str, tuple[OcrWord, ...]]
    text_by_area: dict[str, str]
    ambiguous: bool = False


def _bounds(regions: tuple[OcrRegion, ...]) -> OcrRect:
    return OcrRect(
        min(region.rect.x0 for region in regions),
        min(region.rect.y0 for region in regions),
        max(region.rect.x1 for region in regions),
        max(region.rect.y1 for region in regions),
    )


def _pixel_dimensions(rect: OcrRect, dpi: int, scale: float | None) -> tuple[int, int]:
    factor = float(scale) if scale is not None else float(dpi) / 72.0
    if not math.isfinite(factor) or factor <= 0:
        raise ValueError("OCR render scale must be finite and positive")
    return (
        max(1, int(math.ceil(rect.width * factor))),
        max(1, int(math.ceil(rect.height * factor))),
    )


def _make_group(
    regions: tuple[OcrRegion, ...],
    dpi: int,
    scale: float | None,
) -> OcrRegionGroup:
    bounds = _bounds(regions)
    width, height = _pixel_dimensions(bounds, dpi, scale)
    return OcrRegionGroup(regions, bounds, width, height)


def _near(left: OcrRect, right: OcrRect, maximum_gap: float) -> bool:
    horizontal = max(left.x0 - right.x1, right.x0 - left.x1, 0.0)
    vertical = max(left.y0 - right.y1, right.y0 - left.y1, 0.0)
    return horizontal <= maximum_gap and vertical <= maximum_gap


def _within_budget(
    group: OcrRegionGroup,
    *,
    max_pixels: int,
    max_width: int,
    max_height: int,
) -> bool:
    return (
        group.pixel_width <= max_width
        and group.pixel_height <= max_height
        and group.pixel_count <= max_pixels
    )


def plan_region_groups(
    regions: tuple[OcrRegion, ...],
    *,
    dpi: int,
    scale: float | None,
    max_pixels: int,
    max_width: int,
    max_height: int,
    max_gap_points: float,
    max_regions_per_group: int = 12,
) -> tuple[OcrRegionGroup, ...]:
    if not regions:
        return ()
    if len({region.area_id for region in regions}) != len(regions):
        raise ValueError("OCR region IDs must be unique")
    if min(max_pixels, max_width, max_height, max_regions_per_group) <= 0:
        raise ValueError("OCR grouping limits must be positive")
    if not math.isfinite(max_gap_points) or max_gap_points < 0:
        raise ValueError("OCR grouping gap must be finite and non-negative")
    ordered = tuple(
        sorted(regions, key=lambda item: (item.rect.y0, item.rect.x0, item.area_id))
    )
    groups: list[OcrRegionGroup] = []
    current: tuple[OcrRegion, ...] = ()
    for region in ordered:
        if not current:
            current = (region,)
            continue
        candidate_regions = current + (region,)
        candidate = _make_group(candidate_regions, dpi, scale)
        if (
            len(candidate_regions) <= max_regions_per_group
            and _near(_bounds(current), region.rect, max_gap_points)
            and _within_budget(
                candidate,
                max_pixels=max_pixels,
                max_width=max_width,
                max_height=max_height,
            )
        ):
            current = candidate_regions
        else:
            groups.append(_make_group(current, dpi, scale))
            current = (region,)
    if current:
        groups.append(_make_group(current, dpi, scale))
    return tuple(groups)


def _intersection_area(left: OcrRect, right: OcrRect) -> float:
    width = max(0.0, min(left.x1, right.x1) - max(left.x0, right.x0))
    height = max(0.0, min(left.y1, right.y1) - max(left.y0, right.y0))
    return width * height


def _line_text(words: tuple[OcrWord, ...]) -> str:
    lines: list[list[str]] = []
    previous: tuple[int, int, int] | None = None
    for word in words:
        key = (word.block_number, word.paragraph_number, word.line_number)
        if key != previous:
            lines.append([])
            previous = key
        lines[-1].append(word.text)
    return "\n".join(" ".join(line) for line in lines)


def assign_words_to_regions(
    words: tuple[OcrWord, ...],
    regions: tuple[OcrRegion, ...],
) -> RegionAssignment:
    assigned: dict[str, list[OcrWord]] = {region.area_id: [] for region in regions}
    for word in words:
        area = word.rect.width * word.rect.height
        if area <= 0:
            return RegionAssignment({}, {}, ambiguous=True)
        intersections = [
            (region, _intersection_area(word.rect, region.rect))
            for region in regions
        ]
        intersections = [item for item in intersections if item[1] > 0]
        if len(intersections) > 1:
            return RegionAssignment({}, {}, ambiguous=True)
        if not intersections:
            continue
        region, overlap = intersections[0]
        center_x = (word.rect.x0 + word.rect.x1) / 2
        center_y = (word.rect.y0 + word.rect.y1) / 2
        center_inside = (
            region.rect.x0 <= center_x <= region.rect.x1
            and region.rect.y0 <= center_y <= region.rect.y1
        )
        if not center_inside and overlap / area < 0.8:
            return RegionAssignment({}, {}, ambiguous=True)
        assigned[region.area_id].append(word)
    frozen = {area_id: tuple(values) for area_id, values in assigned.items()}
    return RegionAssignment(
        frozen,
        {area_id: _line_text(values) for area_id, values in frozen.items()},
    )
