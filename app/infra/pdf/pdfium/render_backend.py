from __future__ import annotations

import io
import math

from PIL import Image

from app.infra.pdf.contracts import PdfDocumentSession
from app.infra.pdf.errors import InvalidClipError, RenderingError
from app.infra.pdf.models import PdfRect, RenderedImage
from app.infra.pdf.pdfium.common import require_session
from app.infra.pdf.pdfium.coordinates import PdfiumCoordinateSpace


class PdfiumRenderCapability:
    def render_page(
        self,
        document: PdfDocumentSession,
        page_number: int,
        *,
        scale: float,
    ) -> RenderedImage:
        value = self._positive_scale(scale, label="scale")
        session = require_session(document)
        try:
            with session.open_page(page_number) as page:
                bitmap = page.render(
                    scale=value,
                    rev_byteorder=True,
                    fill_color=(255, 255, 255, 255),
                )
                image = self._owned_pil(bitmap)
                return self._owned_pixels(image)
        except RenderingError:
            raise
        except Exception as exc:
            raise RenderingError(f"Could not render page {page_number}") from exc

    def render_region(
        self,
        document: PdfDocumentSession,
        page_number: int,
        rect: PdfRect,
        *,
        dpi: int,
        scale: float | None,
    ) -> RenderedImage:
        value = (
            self._positive_scale(scale, label="scale")
            if scale is not None
            else self._positive_scale(float(dpi) / 72.0, label="dpi")
        )
        session = require_session(document)
        try:
            with session.open_page(page_number) as page:
                space = self._coordinate_space(page)
                effective = rect.intersect(space.app_bounds)
                if effective is None:
                    raise InvalidClipError(f"Empty or invalid PDF clip: {rect.to_tuple()}")
                bitmap = page.render(
                    scale=value,
                    crop=space.render_crop(effective),
                    rev_byteorder=True,
                    fill_color=(255, 255, 255, 255),
                )
                image = self._owned_pil(bitmap)
                payload = self._png_bytes(image)
                return RenderedImage(payload, image.width, image.height)
        except (InvalidClipError, RenderingError):
            raise
        except Exception as exc:
            raise RenderingError(
                f"Could not render page {page_number} region {rect.to_tuple()}"
            ) from exc

    @staticmethod
    def _positive_scale(value: float, *, label: str) -> float:
        try:
            numeric = float(value)
        except (TypeError, ValueError) as exc:
            raise RenderingError(f"Render {label} must be a positive finite number") from exc
        if not math.isfinite(numeric) or numeric <= 0:
            raise RenderingError(f"Render {label} must be a positive finite number")
        return numeric

    @staticmethod
    def _coordinate_space(page) -> PdfiumCoordinateSpace:
        return PdfiumCoordinateSpace.from_page(page)

    @staticmethod
    def _owned_pil(bitmap) -> Image.Image:
        try:
            source = bitmap.to_pil()
            mode = "RGBA" if "A" in source.mode else "RGB"
            return source.convert(mode).copy()
        finally:
            bitmap.close()

    @staticmethod
    def _png_bytes(image: Image.Image) -> bytes:
        output = io.BytesIO()
        image.save(output, format="PNG", optimize=False, compress_level=6)
        return output.getvalue()

    @staticmethod
    def _owned_pixels(image: Image.Image) -> RenderedImage:
        channels = 4 if image.mode == "RGBA" else 3
        return RenderedImage(
            data=image.tobytes(),
            width=image.width,
            height=image.height,
            mime_type="application/octet-stream",
            pixel_format="rgba8888" if channels == 4 else "rgb888",
            stride=image.width * channels,
        )
