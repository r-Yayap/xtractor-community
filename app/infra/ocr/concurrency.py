from __future__ import annotations

import os
from pathlib import Path
import threading
import time
from typing import BinaryIO


class CrashSafeFileGate:
    """Cross-process admission gate whose leases die with their owner process.

    Each slot is an OS-locked file. Unlike a multiprocessing semaphore, the
    kernel releases these locks if an extraction worker is killed or crashes.
    Instances are spawn-picklable because open handles are kept process-local.
    """

    def __init__(self, directory: Path, slots: int) -> None:
        if slots <= 0:
            raise ValueError("OCR admission slots must be positive")
        self._directory = Path(directory)
        self._slots = slots
        self._state_lock = threading.Lock()
        self._leases: list[BinaryIO] = []

    @classmethod
    def create(cls, directory: Path, slots: int) -> "CrashSafeFileGate":
        directory.mkdir(parents=True, exist_ok=False)
        for index in range(slots):
            (directory / f"slot-{index}.lock").write_bytes(b"0")
        return cls(directory, slots)

    def __getstate__(self) -> dict[str, object]:
        return {"_directory": self._directory, "_slots": self._slots}

    def __setstate__(self, state: dict[str, object]) -> None:
        self._directory = Path(state["_directory"])
        self._slots = int(state["_slots"])
        self._state_lock = threading.Lock()
        self._leases = []

    def _process_state(self) -> tuple[threading.Lock, list[BinaryIO]]:
        return self._state_lock, self._leases

    @staticmethod
    def _try_lock(handle: BinaryIO) -> bool:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                return False
        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    @staticmethod
    def _unlock(handle: BinaryIO) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def acquire(self, *args, **kwargs) -> bool:
        timeout = kwargs.get("timeout")
        if timeout is None and args:
            timeout = args[0]
        deadline = None if timeout is None else time.monotonic() + float(timeout)
        state_lock, leases = self._process_state()
        while True:
            with state_lock:
                for index in range(self._slots):
                    handle = (self._directory / f"slot-{index}.lock").open("r+b")
                    if self._try_lock(handle):
                        leases.append(handle)
                        return True
                    handle.close()
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(0.01)

    def release(self) -> None:
        state_lock, leases = self._process_state()
        with state_lock:
            if not leases:
                raise RuntimeError("OCR admission lease was not held")
            handle = leases.pop()
            try:
                self._unlock(handle)
            finally:
                handle.close()
