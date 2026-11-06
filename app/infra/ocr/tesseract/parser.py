from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass

from app.infra.ocr.errors import OcrSubsystemError
from app.infra.ocr.models import OcrDiagnostic, OcrDiagnosticCode


_REQUIRED_FIELDS = (
    "level",
    "page_num",
    "block_num",
    "par_num",
    "line_num",
    "word_num",
    "left",
    "top",
    "width",
    "height",
    "conf",
    "text",
)


@dataclass(frozen=True, slots=True)
class PixelRect:
    x0: int
    y0: int
    x1: int
    y1: int

    def to_tuple(self) -> tuple[int, int, int, int]:
        return (self.x0, self.y0, self.x1, self.y1)


@dataclass(frozen=True, slots=True)
class TsvWord:
    text: str
    confidence: float
    pixel_rect: PixelRect
    block_number: int
    paragraph_number: int
    line_number: int
    word_number: int


def _failure(code: OcrDiagnosticCode, message: str) -> OcrSubsystemError:
    return OcrSubsystemError(OcrDiagnostic(code, message))


def parse_tsv(
    payload: bytes,
    *,
    max_output_bytes: int,
    image_width: int | None = None,
    image_height: int | None = None,
) -> tuple[TsvWord, ...]:
    if not payload:
        raise _failure(OcrDiagnosticCode.OUTPUT_MISSING, "OCR produced no TSV output")
    if len(payload) > max_output_bytes:
        raise _failure(
            OcrDiagnosticCode.OUTPUT_TOO_LARGE,
            "OCR TSV output exceeded its configured limit",
        )
    try:
        text_payload = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise _failure(
            OcrDiagnosticCode.INVALID_ENCODING,
            "OCR TSV output was not valid UTF-8",
        ) from exc

    reader = csv.DictReader(io.StringIO(text_payload, newline=""), delimiter="\t")
    if tuple(reader.fieldnames or ()) != _REQUIRED_FIELDS:
        raise _failure(
            OcrDiagnosticCode.MALFORMED_OUTPUT,
            "OCR TSV output had an unexpected header",
        )

    words: list[TsvWord] = []
    try:
        for row in reader:
            if None in row or any(row.get(field) is None for field in _REQUIRED_FIELDS):
                raise ValueError("partial TSV row")
            level = int(row["level"])
            page_number = int(row["page_num"])
            hierarchy = (
                int(row["block_num"]),
                int(row["par_num"]),
                int(row["line_num"]),
                int(row["word_num"]),
            )
            left = int(row["left"])
            top = int(row["top"])
            width = int(row["width"])
            height = int(row["height"])
            confidence = float(row["conf"])
            if level not in {1, 2, 3, 4, 5} or page_number != 1:
                raise ValueError("invalid TSV page or level")
            if min(left, top, width, height, *hierarchy) < 0:
                raise ValueError("invalid TSV numeric field")
            if level != 5:
                continue
            value = row["text"].strip()
            if not value:
                continue
            if width == 0 or height == 0:
                raise ValueError("invalid TSV geometry")
            right = left + width
            bottom = top + height
            if image_width is not None and right > image_width:
                raise ValueError("TSV word exceeds image width")
            if image_height is not None and bottom > image_height:
                raise ValueError("TSV word exceeds image height")
            if not math.isfinite(confidence) or not 0 <= confidence <= 100:
                raise ValueError("invalid TSV confidence")
            words.append(
                TsvWord(
                    value,
                    confidence,
                    PixelRect(left, top, right, bottom),
                    hierarchy[0],
                    hierarchy[1],
                    hierarchy[2],
                    hierarchy[3],
                )
            )
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise _failure(
            OcrDiagnosticCode.MALFORMED_OUTPUT,
            "OCR TSV output contained an invalid row",
        ) from exc
    return tuple(words)
