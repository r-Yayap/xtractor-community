from __future__ import annotations


OCR_MARKER = "_OCR_"


def decode_ocr_value(value: object) -> tuple[object, bool]:
    """Return the visible cell value and whether it came from OCR."""
    if isinstance(value, str) and value.startswith(OCR_MARKER):
        return value[len(OCR_MARKER) :], True
    return value, False
