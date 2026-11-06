from __future__ import annotations

from collections.abc import Callable
import time

from app.infra.ocr.errors import OcrSubsystemError
from app.infra.ocr.models import (
    OcrPolicy,
    OcrRect,
    OcrRequest,
    OcrResult,
    OcrWord,
)
from app.infra.ocr.tesseract.parser import TsvWord, parse_tsv
from app.infra.ocr.tesseract.process import run_owned_process
from app.infra.ocr.tesseract.runtime import OcrRuntime


class TesseractEngine:
    def __init__(self, runtime: OcrRuntime, policy: OcrPolicy) -> None:
        self._runtime = runtime
        self._policy = policy

    def recognize(
        self,
        request: OcrRequest,
        *,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> OcrResult:
        started = time.perf_counter()
        command = (
            str(self._runtime.executable),
            "stdin",
            "stdout",
            "--tessdata-dir",
            str(self._runtime.tessdata_dir),
            "-l",
            request.language,
            "--psm",
            str(request.segmentation_mode.value),
            "tsv",
        )
        try:
            execution = run_owned_process(
                command,
                request.image.data,
                policy=self._policy,
                is_cancelled=is_cancelled,
            )
            parsed = parse_tsv(
                execution.stdout,
                max_output_bytes=self._policy.max_output_bytes,
                image_width=request.image.width,
                image_height=request.image.height,
            )
        except OcrSubsystemError as exc:
            return OcrResult.failure(
                exc.diagnostic,
                elapsed_ms=(time.perf_counter() - started) * 1000,
                attempts=1,
            )
        return OcrResult.success(
            _line_text(parsed),
            words=tuple(_neutral_word(word, request) for word in parsed),
            elapsed_ms=execution.elapsed_ms,
            attempts=1,
            runtime_version=self._runtime.version,
            peak_child_memory_bytes=execution.peak_memory_bytes,
        )


def _neutral_word(word: TsvWord, request: OcrRequest) -> OcrWord:
    pixel = word.pixel_rect
    rect = request.transform.map_rect(
        OcrRect(float(pixel.x0), float(pixel.y0), float(pixel.x1), float(pixel.y1))
    )
    return OcrWord(
        text=word.text,
        rect=rect,
        confidence=word.confidence,
        block_number=word.block_number,
        paragraph_number=word.paragraph_number,
        line_number=word.line_number,
        word_number=word.word_number,
    )


def _line_text(words: tuple[TsvWord, ...]) -> str:
    lines: list[list[str]] = []
    previous: tuple[int, int, int] | None = None
    for word in words:
        current = (word.block_number, word.paragraph_number, word.line_number)
        if current != previous:
            lines.append([])
            previous = current
        lines[-1].append(word.text)
    return "\n".join(" ".join(line) for line in lines)
