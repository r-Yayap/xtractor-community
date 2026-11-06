from __future__ import annotations

from app.common.geometry import adjust_coordinates_for_rotation
from app.domain.extraction_summary import find_reference_column_index
from app.domain.models import AreaSpec, ExtractionRequest
from app.infra.pdf.models import PdfPageInfo, PdfRect
from app.services.extraction.contracts import (
    AreaExtractionPlan,
    ExtractionPlan,
    PageAreaPlan,
    PageExtractionPlan,
)
from app.domain.area_coordinates import CoordinateBasis, CoordinateTransform


def effective_area_options(
    mode: str,
    area: AreaSpec,
    ocr_enabled: bool = True,
) -> tuple[bool, bool]:
    if mode == "OCR-All":
        return (ocr_enabled, False)
    if mode == "Text1st+Image-beta":
        return (False, True)
    if mode == "Custom":
        return (ocr_enabled and bool(area.use_ocr), bool(area.use_image))
    return (False, False)


def build_extraction_plan(
    request: ExtractionRequest,
) -> ExtractionPlan:
    areas = tuple(
        AreaExtractionPlan(
            index=index,
            title=area.title or f"Area {index + 1}",
            rect=tuple(float(value) for value in area.rect),
            use_ocr=effective_area_options(
                request.ocr.mode, area, request.ocr.enabled
            )[0],
            use_image=effective_area_options(
                request.ocr.mode, area, request.ocr.enabled
            )[1],
        )
        for index, area in enumerate(request.areas)
    )
    coordinate_basis = request.coordinate_basis
    reference_width = (
        coordinate_basis.width
        if coordinate_basis is not None
        else request.reference_page_width
    )
    reference_height = (
        coordinate_basis.height
        if coordinate_basis is not None
        else request.reference_page_height
    )
    return ExtractionPlan(
        areas=areas,
        ocr_mode=request.ocr.mode,
        ocr_enabled=request.ocr.enabled,
        ocr_dpi=max(1, int(request.ocr.dpi or 150)),
        ocr_scale=request.ocr.scale,
        pdf_root=str(request.pdf_root) if request.pdf_root else ".",
        text_output=request.text_output,
        reference_column_index=find_reference_column_index(
            [area.title for area in request.areas]
        ),
        reference_page_width=reference_width,
        reference_page_height=reference_height,
        coordinate_basis=coordinate_basis,
    )


def build_page_plan(
    plan: ExtractionPlan,
    page_info: PdfPageInfo,
) -> PageExtractionPlan:
    width = page_info.width
    height = page_info.height
    area_rects = tuple(area.rect for area in plan.areas)
    coordinate_basis = plan.coordinate_basis
    if coordinate_basis is not None:
        transform = CoordinateTransform(coordinate_basis, CoordinateBasis(width, height))
        area_rects = tuple(transform.to_target_rect(rect) for rect in area_rects)
    page_areas = tuple(
        PageAreaPlan(
            area=area,
            source_rect=area_rects[index],
            text_rect=PdfRect.from_value(
                adjust_coordinates_for_rotation(
                    area_rects[index],
                    page_info.rotation_degrees,
                    height,
                    width,
                )
            ),
            image_rect=_clip(PdfRect.from_value(area_rects[index]), page_info.bounds),
        )
        for index, area in enumerate(plan.areas)
    )
    different = False
    reference_width = (
        coordinate_basis.width
        if coordinate_basis is not None
        else plan.reference_page_width
    )
    reference_height = (
        coordinate_basis.height
        if coordinate_basis is not None
        else plan.reference_page_height
    )
    if reference_width is not None and reference_height is not None:
        different = (
            abs(width - reference_width) > 1.0
            or abs(height - reference_height) > 1.0
        )
    return PageExtractionPlan(
        page_number=page_info.page_number,
        bounds=page_info.bounds,
        width=width,
        height=height,
        rotation_degrees=page_info.rotation_degrees,
        source_rotation_degrees=page_info.source_rotation_degrees,
        areas=page_areas,
        different_page_size=different,
    )


def _clip(rect: PdfRect, bounds: PdfRect) -> PdfRect | None:
    clipped = rect.intersect(bounds)
    return clipped if clipped is not None and clipped.is_valid(minimum_size=1e-3) else None
