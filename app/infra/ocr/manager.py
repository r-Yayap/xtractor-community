from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import hashlib
import logging
import struct
import threading
import time
from typing import Protocol

from app.infra.ocr.contracts import OcrEngine
from app.infra.ocr.errors import OcrSubsystemError
from app.infra.ocr.models import (
    OcrDiagnostic,
    OcrDiagnosticCode,
    OcrOutcome,
    OcrPolicy,
    OcrRequest,
    OcrResult,
)


class _AdmissionGate(Protocol):
    def acquire(self, *args, **kwargs) -> bool: ...

    def release(self) -> None: ...


_shared_gate: _AdmissionGate | None = None
_shared_cancel = None


def initialize_ocr_worker(shared_gate: _AdmissionGate | None, shared_cancel=None) -> None:
    """Install spawn-safe admission and cancellation primitives in a worker."""

    global _shared_gate, _shared_cancel
    _shared_gate = shared_gate
    _shared_cancel = shared_cancel


class OcrManager:
    def __init__(
        self,
        engine: OcrEngine,
        policy: OcrPolicy,
        *,
        shared_gate: _AdmissionGate | None = None,
    ) -> None:
        self._engine = engine
        self._policy = policy
        self._local_gate = threading.BoundedSemaphore(policy.max_concurrent_operations)
        self._configured_shared_gate = shared_gate

    def _validate_request(self, request: OcrRequest) -> OcrDiagnostic | None:
        image = request.image
        if (
            len(image.data) < 24
            or image.data[:8] != b"\x89PNG\r\n\x1a\n"
            or image.data[12:16] != b"IHDR"
            or struct.unpack(">I", image.data[8:12])[0] != 13
        ):
            return OcrDiagnostic(
                OcrDiagnosticCode.INVALID_REQUEST,
                "OCR image was not a valid bounded PNG payload",
            )
        actual_width, actual_height = struct.unpack(">II", image.data[16:24])
        if actual_width != image.width or actual_height != image.height:
            return OcrDiagnostic(
                OcrDiagnosticCode.INVALID_REQUEST,
                "OCR image metadata did not match its PNG header",
            )
        if (
            actual_width > self._policy.max_width
            or actual_height > self._policy.max_height
            or actual_width * actual_height > self._policy.max_image_pixels
            or len(image.data) > self._policy.max_input_bytes
        ):
            return OcrDiagnostic(
                OcrDiagnosticCode.INPUT_TOO_LARGE,
                "OCR image exceeded its configured resource policy",
            )
        return None

    @staticmethod
    def _cancelled(is_cancelled: Callable[[], bool] | None) -> bool:
        return is_cancelled is not None and is_cancelled()

    @staticmethod
    def _metadata_hash(value: str | None) -> str | None:
        if value is None:
            return None
        return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()

    def _report(self, request: OcrRequest, result: OcrResult) -> None:
        diagnostics = result.diagnostics
        logging.getLogger(__name__).log(
            logging.DEBUG if result.succeeded else logging.WARNING,
            "OCR operation completed",
            extra={
                "ocr_operation_id": self._metadata_hash(request.operation_id),
                "ocr_source_id": self._metadata_hash(request.source_id),
                "ocr_page_number": request.page_number,
                "ocr_area_id": self._metadata_hash(request.area_id),
                "ocr_outcome": result.outcome.value,
                "ocr_runtime_version": result.runtime_version,
                "ocr_elapsed_ms": round(result.elapsed_ms, 3),
                "ocr_attempts": result.attempts,
                "ocr_peak_child_memory_bytes": result.peak_child_memory_bytes,
                "ocr_exit_code": next(
                    (
                        item.exit_code
                        for item in reversed(diagnostics)
                        if item.exit_code is not None
                    ),
                    None,
                ),
                "ocr_timed_out": any(item.timed_out for item in diagnostics),
                "ocr_retry_count": max(
                    (item.retry_count for item in diagnostics), default=0
                ),
                "ocr_diagnostic_codes": tuple(
                    item.code.value for item in diagnostics
                ),
            },
        )

    def recognize(
        self,
        request: OcrRequest,
        *,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> OcrResult:
        effective_cancelled = is_cancelled
        if _shared_cancel is not None:
            effective_cancelled = lambda: bool(
                (_shared_cancel is not None and _shared_cancel.is_set())
                or (is_cancelled is not None and is_cancelled())
            )
        result = self._recognize(request, is_cancelled=effective_cancelled)
        self._report(request, result)
        return result

    def _acquire(
        self,
        gate: _AdmissionGate,
        deadline: float,
        is_cancelled: Callable[[], bool] | None,
    ) -> OcrDiagnostic | None:
        while True:
            if self._cancelled(is_cancelled):
                return OcrDiagnostic(
                    OcrDiagnosticCode.CONCURRENCY_CANCELLED,
                    "OCR operation was cancelled while waiting for capacity",
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return OcrDiagnostic(
                    OcrDiagnosticCode.CONCURRENCY_TIMEOUT,
                    "OCR operation could not obtain capacity before its deadline",
                )
            if gate.acquire(timeout=min(0.05, remaining)):
                return None

    def _recognize(
        self,
        request: OcrRequest,
        *,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> OcrResult:
        started = time.perf_counter()
        invalid = self._validate_request(request)
        if invalid is not None:
            return OcrResult.failure(invalid, attempts=0)
        if self._cancelled(is_cancelled):
            return OcrResult.failure(
                OcrDiagnostic(OcrDiagnosticCode.CANCELLED, "OCR operation was cancelled"),
                attempts=0,
            )

        deadline = time.monotonic() + self._policy.timeout_seconds
        acquired: list[_AdmissionGate] = []
        shared = self._configured_shared_gate or _shared_gate
        for gate in (self._local_gate, shared):
            if gate is None:
                continue
            diagnostic = self._acquire(gate, deadline, is_cancelled)
            if diagnostic is not None:
                for held in reversed(acquired):
                    held.release()
                return OcrResult.failure(
                    diagnostic,
                    elapsed_ms=(time.perf_counter() - started) * 1000,
                    attempts=0,
                )
            acquired.append(gate)

        diagnostics: list[OcrDiagnostic] = []
        attempts = 0
        try:
            while attempts <= self._policy.max_retries:
                if self._cancelled(is_cancelled):
                    diagnostic = OcrDiagnostic(
                        OcrDiagnosticCode.CANCELLED,
                        "OCR operation was cancelled",
                        retry_count=max(0, attempts - 1),
                    )
                    diagnostics.append(diagnostic)
                    return OcrResult(
                        OcrOutcome.CANCELLED,
                        None,
                        (),
                        tuple(diagnostics),
                        (time.perf_counter() - started) * 1000,
                        attempts,
                    )
                attempts += 1
                try:
                    result = self._engine.recognize(
                        request,
                        is_cancelled=is_cancelled,
                    )
                except OcrSubsystemError as exc:
                    result = OcrResult.failure(exc.diagnostic)
                except Exception as exc:
                    result = OcrResult.failure(
                        OcrDiagnostic(
                            OcrDiagnosticCode.INTERNAL_FAILURE,
                            f"OCR engine failed internally ({type(exc).__name__})",
                        )
                    )
                if result.succeeded:
                    return OcrResult.success(
                        result.text or "",
                        words=result.words,
                        diagnostics=tuple(diagnostics) + result.diagnostics,
                        elapsed_ms=(time.perf_counter() - started) * 1000,
                        attempts=attempts,
                        runtime_version=result.runtime_version,
                        peak_child_memory_bytes=result.peak_child_memory_bytes,
                    )
                current = tuple(
                    replace(item, retry_count=attempts - 1)
                    for item in result.diagnostics
                )
                diagnostics.extend(current)
                retryable = bool(current) and current[-1].retryable
                if not retryable:
                    return OcrResult(
                        result.outcome,
                        None,
                        (),
                        tuple(diagnostics),
                        (time.perf_counter() - started) * 1000,
                        attempts,
                    )
                if attempts > self._policy.max_retries:
                    diagnostics.append(
                        OcrDiagnostic(
                            OcrDiagnosticCode.RETRY_EXHAUSTED,
                            "OCR retry policy was exhausted",
                            retry_count=attempts - 1,
                        )
                    )
                    return OcrResult(
                        OcrOutcome.FAILED,
                        None,
                        (),
                        tuple(diagnostics),
                        (time.perf_counter() - started) * 1000,
                        attempts,
                    )
            raise AssertionError("unreachable OCR retry state")
        finally:
            for gate in reversed(acquired):
                gate.release()
