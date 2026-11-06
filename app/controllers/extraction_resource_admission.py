import threading

from app.domain.extraction_resources import ExtractionResourcePolicy


class ExtractionAdmissionLease:
    """Opaque token representing one successful admission."""

    __slots__ = ()


class ExtractionResourceAdmission:
    def __init__(
        self, policy: ExtractionResourcePolicy | None = None
    ) -> None:
        self._lock = threading.Lock()
        self._policy = policy or ExtractionResourcePolicy()
        self._active_lease: ExtractionAdmissionLease | None = None

    @property
    def capacity(self) -> int:
        return self._policy.max_active_jobs

    @property
    def active_count(self) -> int:
        with self._lock:
            return int(self._active_lease is not None)

    def try_acquire(self) -> ExtractionAdmissionLease | None:
        with self._lock:
            if self._active_lease is not None:
                return None

            lease = ExtractionAdmissionLease()
            self._active_lease = lease
            return lease

    def release(self, lease: ExtractionAdmissionLease | None) -> bool:
        with self._lock:
            if lease is None or lease is not self._active_lease:
                return False

            self._active_lease = None
            return True
