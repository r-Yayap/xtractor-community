from __future__ import annotations

from dataclasses import dataclass

from app.infra.pdf.models import PdfRect


@dataclass(frozen=True, slots=True)
class PdfiumCoordinateSpace:
    media_box: PdfRect
    crop_box: PdfRect
    rotation_degrees: int

    @classmethod
    def from_page(cls, page) -> "PdfiumCoordinateSpace":
        """Resolve effective page geometry without PDFium's Letter fallback."""
        media_value = page.get_mediabox(fallback_ok=False)
        crop_value = page.get_cropbox(fallback_ok=False)
        effective_box = PdfRect.from_value(page.get_bbox())

        if media_value is None:
            # PDFium's box getters do not inherit values from parent /Pages
            # nodes. Its bounding-box API does, so use the effective box for
            # both values when the inherited MediaBox is unavailable.
            media_box = effective_box
            raw_crop_box = effective_box
        else:
            media_box = PdfRect.from_value(media_value)
            raw_crop_box = (
                PdfRect.from_value(crop_value)
                if crop_value is not None
                else effective_box
            )

        crop_box = PdfRect(
            raw_crop_box.x0,
            media_box.y1 - raw_crop_box.y1,
            raw_crop_box.x1,
            media_box.y1 - raw_crop_box.y0,
        )
        return cls.from_boxes(
            media_box,
            crop_box,
            int(page.get_rotation()) % 360,
        )

    @classmethod
    def from_boxes(
        cls,
        media_box: PdfRect,
        crop_box: PdfRect,
        rotation: int,
    ) -> "PdfiumCoordinateSpace":
        normalized_rotation = int(rotation) % 360
        if normalized_rotation not in {0, 90, 180, 270}:
            raise ValueError(f"Unsupported PDF page rotation: {rotation!r}")
        media = media_box.normalized()
        application_media = PdfRect(media.x0, 0.0, media.x1, media.height)
        crop = crop_box.normalized().intersect(application_media)
        if crop is None:
            raise ValueError("PDF CropBox does not intersect MediaBox")
        return cls(media, crop, normalized_rotation)

    @property
    def unrotated_width(self) -> float:
        return self.crop_box.width

    @property
    def unrotated_height(self) -> float:
        return self.crop_box.height

    @property
    def app_bounds(self) -> PdfRect:
        if self.rotation_degrees in {90, 270}:
            return PdfRect(0, 0, self.unrotated_height, self.unrotated_width)
        return PdfRect(0, 0, self.unrotated_width, self.unrotated_height)

    def app_to_pdfium(self, rect: PdfRect) -> PdfRect:
        return self._map_rect(rect, self._app_point_to_pdfium)

    def pdfium_to_app(self, rect: PdfRect) -> PdfRect:
        return self._map_rect(rect, self._pdfium_point_to_app)

    def render_crop(self, rect: PdfRect) -> tuple[float, float, float, float]:
        source = rect.normalized()
        bounds = self.app_bounds
        return (
            source.x0,
            bounds.y1 - source.y1,
            bounds.x1 - source.x1,
            source.y0,
        )

    def _app_point_to_pdfium(self, x: float, y: float) -> tuple[float, float]:
        width = self.unrotated_width
        height = self.unrotated_height
        if self.rotation_degrees == 0:
            u, v = x, y
        elif self.rotation_degrees == 90:
            u, v = y, height - x
        elif self.rotation_degrees == 180:
            u, v = width - x, height - y
        else:
            u, v = width - y, x
        source_x = self.crop_box.x0 + u
        source_top_y = self.crop_box.y0 + v
        return source_x, self.media_box.y1 - source_top_y

    def _pdfium_point_to_app(self, x: float, y: float) -> tuple[float, float]:
        u = x - self.crop_box.x0
        source_top_y = self.media_box.y1 - y
        v = source_top_y - self.crop_box.y0
        width = self.unrotated_width
        height = self.unrotated_height
        if self.rotation_degrees == 0:
            return u, v
        if self.rotation_degrees == 90:
            return height - v, u
        if self.rotation_degrees == 180:
            return width - u, height - v
        return v, width - u

    @staticmethod
    def _map_rect(rect: PdfRect, mapper) -> PdfRect:
        source = rect.normalized()
        points = (
            mapper(source.x0, source.y0),
            mapper(source.x1, source.y0),
            mapper(source.x0, source.y1),
            mapper(source.x1, source.y1),
        )
        xs = tuple(point[0] for point in points)
        ys = tuple(point[1] for point in points)
        return PdfRect(min(xs), min(ys), max(xs), max(ys))
