from __future__ import annotations

from collections.abc import Callable, Sequence
import hashlib
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from app.infra.ocr.errors import OcrSubsystemError
from app.infra.ocr.models import OcrDiagnostic, OcrDiagnosticCode, OcrPolicy


@dataclass(frozen=True, slots=True)
class ProcessExecution:
    stdout: bytes
    stderr: bytes
    exit_code: int
    elapsed_ms: float
    peak_memory_bytes: int = 0

    @property
    def stderr_sha256(self) -> str:
        return hashlib.sha256(self.stderr).hexdigest()


def popen_options() -> dict[str, object]:
    options: dict[str, object] = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "bufsize": 0,
    }
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        options["start_new_session"] = True
    return options


def _diagnostic(
    code: OcrDiagnosticCode,
    message: str,
    *,
    retryable: bool = False,
    exit_code: int | None = None,
    timed_out: bool = False,
) -> OcrSubsystemError:
    return OcrSubsystemError(
        OcrDiagnostic(
            code,
            message,
            retryable=retryable,
            exit_code=exit_code,
            timed_out=timed_out,
        )
    )


class _WindowsOwnerJob:
    """Kill-on-close Job Object joined by the owner before any OCR spawn."""

    def __init__(self) -> None:
        self._handle: int | None = None
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        )
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = (
            wintypes.HANDLE,
            wintypes.HANDLE,
        )
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.GetCurrentProcess.argtypes = ()
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0x00002000
        if not kernel32.SetInformationJobObject(
            handle,
            9,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise ctypes.WinError(error)
        self._handle = int(handle)
        self._kernel32 = kernel32

    def assign_current_process(self) -> None:
        if self._handle is None:
            return
        import ctypes
        from ctypes import wintypes

        if not self._kernel32.AssignProcessToJobObject(
            wintypes.HANDLE(self._handle), self._kernel32.GetCurrentProcess()
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    def assign_process(self, process: subprocess.Popen[bytes]) -> None:
        if self._handle is None:
            return
        import ctypes
        from ctypes import wintypes

        process_handle = wintypes.HANDLE(int(process._handle))  # type: ignore[attr-defined]
        if not self._kernel32.AssignProcessToJobObject(
            wintypes.HANDLE(self._handle), process_handle
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        if self._handle is not None:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


_windows_owner_job_lock = threading.Lock()
_windows_owner_job: _WindowsOwnerJob | None = None


def _ensure_windows_owner_job() -> None:
    """Contain descendants before creation, avoiding Popen-to-assignment races."""

    if os.name != "nt":
        return
    global _windows_owner_job
    with _windows_owner_job_lock:
        if _windows_owner_job is not None:
            return
        job = _WindowsOwnerJob()
        try:
            job.assign_current_process()
        except OSError:
            job.close()
            raise
        _windows_owner_job = job


def _read_bounded(
    stream: BinaryIO,
    output: bytearray,
    maximum: int,
    overflow: threading.Event,
    read_error: threading.Event,
) -> None:
    try:
        while True:
            chunk = stream.read(min(64 * 1024, maximum + 1 - len(output)))
            if not chunk:
                return
            output.extend(chunk)
            if len(output) > maximum:
                overflow.set()
                return
    except (OSError, ValueError):
        read_error.set()


def _write_input(stream: BinaryIO, payload: bytes) -> None:
    try:
        stream.write(payload)
        stream.flush()
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def _signal_process(process: subprocess.Popen[bytes], *, force: bool) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        (process.kill if force else process.terminate)()
        return
    try:
        os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        (process.kill if force else process.terminate)()


def _stop_process(process: subprocess.Popen[bytes], grace_seconds: float) -> None:
    if process.poll() is not None:
        return
    _signal_process(process, force=False)
    try:
        process.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    _signal_process(process, force=True)
    try:
        process.wait(timeout=max(1.0, grace_seconds))
    except subprocess.TimeoutExpired:
        # There is no further escalation available. The Job Object/process group
        # remains owned until finally cleanup below.
        pass


def _close_pipe(stream: BinaryIO | None) -> None:
    if stream is not None:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def _process_memory_bytes(process: subprocess.Popen[bytes]) -> int:
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.c_ulong),
                    ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(counters)
            get_process_memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
            get_process_memory_info.argtypes = (
                wintypes.HANDLE,
                ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
                wintypes.DWORD,
            )
            get_process_memory_info.restype = wintypes.BOOL
            if get_process_memory_info(
                wintypes.HANDLE(int(process._handle)),  # type: ignore[attr-defined]
                ctypes.byref(counters),
                wintypes.DWORD(counters.cb),
            ):
                return int(counters.PeakWorkingSetSize)
        except (AttributeError, OSError, TypeError, ValueError, ctypes.ArgumentError):
            return 0
        return 0
    try:
        for line in (Path(f"/proc/{process.pid}/status")).read_text().splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def run_owned_process(
    command: Sequence[str],
    input_bytes: bytes,
    *,
    policy: OcrPolicy,
    is_cancelled: Callable[[], bool] | None = None,
) -> ProcessExecution:
    if not command or not all(isinstance(value, str) and value for value in command):
        raise _diagnostic(
            OcrDiagnosticCode.INVALID_REQUEST,
            "OCR process command was invalid",
        )
    if not isinstance(input_bytes, bytes):
        raise _diagnostic(
            OcrDiagnosticCode.INVALID_REQUEST,
            "OCR process input was not owned bytes",
        )
    if len(input_bytes) > policy.max_input_bytes:
        raise _diagnostic(
            OcrDiagnosticCode.INPUT_TOO_LARGE,
            "OCR process input exceeded its configured limit",
        )
    if is_cancelled is not None and is_cancelled():
        raise _diagnostic(OcrDiagnosticCode.CANCELLED, "OCR operation was cancelled")

    process: subprocess.Popen[bytes] | None = None
    operation_job: _WindowsOwnerJob | None = None
    threads: list[threading.Thread] = []
    stdout = bytearray()
    stderr = bytearray()
    overflow = threading.Event()
    read_error = threading.Event()
    started = time.perf_counter()
    pending_error: OcrSubsystemError | None = None
    peak_memory_bytes = 0
    try:
        try:
            _ensure_windows_owner_job()
            operation_job = _WindowsOwnerJob()
            process = subprocess.Popen(tuple(command), **popen_options())
            operation_job.assign_process(process)
        except OSError as exc:
            if process is not None:
                _stop_process(process, policy.termination_grace_seconds)
            raise _diagnostic(
                OcrDiagnosticCode.PROCESS_LAUNCH_FAILED,
                "OCR process could not be started safely",
                retryable=True,
            ) from exc
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        threads = [
            threading.Thread(
                target=_write_input,
                args=(process.stdin, input_bytes),
                name="ocr-stdin",
                daemon=True,
            ),
            threading.Thread(
                target=_read_bounded,
                args=(
                    process.stdout,
                    stdout,
                    policy.max_output_bytes,
                    overflow,
                    read_error,
                ),
                name="ocr-stdout",
                daemon=True,
            ),
            threading.Thread(
                target=_read_bounded,
                args=(
                    process.stderr,
                    stderr,
                    policy.max_output_bytes,
                    overflow,
                    read_error,
                ),
                name="ocr-stderr",
                daemon=True,
            ),
        ]
        for thread in threads:
            thread.start()

        deadline = time.monotonic() + policy.timeout_seconds
        while process.poll() is None:
            peak_memory_bytes = max(peak_memory_bytes, _process_memory_bytes(process))
            if overflow.is_set():
                pending_error = _diagnostic(
                    OcrDiagnosticCode.OUTPUT_TOO_LARGE,
                    "OCR process output exceeded its configured limit",
                )
                break
            if is_cancelled is not None and is_cancelled():
                pending_error = _diagnostic(
                    OcrDiagnosticCode.CANCELLED,
                    "OCR operation was cancelled",
                )
                break
            if time.monotonic() >= deadline:
                pending_error = _diagnostic(
                    OcrDiagnosticCode.PROCESS_TIMEOUT,
                    "OCR process exceeded its deadline",
                    retryable=True,
                    timed_out=True,
                )
                break
            time.sleep(0.01)
        if pending_error is not None:
            _stop_process(process, policy.termination_grace_seconds)
        else:
            process.wait()
        for thread in threads:
            thread.join(timeout=max(1.0, policy.termination_grace_seconds))
        if pending_error is None and any(thread.is_alive() for thread in threads):
            pending_error = _diagnostic(
                OcrDiagnosticCode.PROCESS_TERMINATED,
                "OCR process streams did not close completely",
                retryable=True,
            )
        if pending_error is None and read_error.is_set():
            pending_error = _diagnostic(
                OcrDiagnosticCode.PROCESS_TERMINATED,
                "OCR process output could not be read completely",
                retryable=True,
            )
        if overflow.is_set() and pending_error is None:
            pending_error = _diagnostic(
                OcrDiagnosticCode.OUTPUT_TOO_LARGE,
                "OCR process output exceeded its configured limit",
            )
        if pending_error is not None:
            raise pending_error
        exit_code = int(process.returncode)
        if exit_code != 0:
            raise _diagnostic(
                OcrDiagnosticCode.PROCESS_EXIT_FAILED,
                "OCR process exited unsuccessfully",
                retryable=True,
                exit_code=exit_code,
            )
        return ProcessExecution(
            stdout=bytes(stdout),
            stderr=bytes(stderr),
            exit_code=exit_code,
            elapsed_ms=(time.perf_counter() - started) * 1000,
            peak_memory_bytes=peak_memory_bytes,
        )
    finally:
        # Closing the per-operation job first kills any descendant that inherited
        # a captured pipe. That lets reader threads reach EOF instead of blocking
        # while cleanup attempts to close a handle with an outstanding ReadFile.
        if operation_job is not None:
            operation_job.close()
        if process is not None:
            _stop_process(process, policy.termination_grace_seconds)
            _close_pipe(process.stdin)
            _close_pipe(process.stdout)
            _close_pipe(process.stderr)
            try:
                process.wait(timeout=max(1.0, policy.termination_grace_seconds))
            except subprocess.TimeoutExpired:
                pass
        for thread in threads:
            thread.join(timeout=0.2)
