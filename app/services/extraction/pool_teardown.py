"""Bounded, spawn-safe cleanup for extraction worker pools.

This module intentionally knows as little as possible about ``multiprocessing``.
Keeping the process seams here makes cancellation cleanup safe to call from the
coordinator without importing Qt or serialising coordinator state into workers.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Callable, Iterable


_LOGGER = logging.getLogger(__name__)
SAFE_POOL_TEARDOWN_ERROR_MESSAGE = (
    "Extraction worker processes could not be released cleanly within the "
    "bounded shutdown policy."
)


class PoolTeardownReason(str, Enum):
    SUCCESS = "success"
    CANCELLED = "cancelled"
    EXCEPTION = "exception"


@dataclass(frozen=True, slots=True)
class PoolTeardownPolicy:
    terminate_call_timeout_seconds: float = 1.0
    terminate_reap_timeout_seconds: float = 2.0
    kill_reap_timeout_seconds: float = 2.0
    watcher_join_timeout_seconds: float = 1.0
    poll_interval_seconds: float = 0.05


DEFAULT_POOL_TEARDOWN_POLICY = PoolTeardownPolicy()


@dataclass(frozen=True, slots=True)
class PoolTeardownReport:
    """A sanitised account of the pool's bounded cleanup attempt."""

    reason: PoolTeardownReason
    worker_count: int | None
    alive_before_terminate: int | None
    alive_after_terminate: int | None
    alive_after_kill: int | None
    terminate_call_timed_out: bool
    terminate_reap_timed_out: bool
    kill_reap_timed_out: bool
    kill_requested: int
    cleanup_diagnostics: tuple[str, ...] = field(default_factory=tuple)
    cleanup_trustworthy: bool = True


class WorkerPoolTeardownError(RuntimeError):
    """Raised when bounded worker cleanup cannot establish a safe terminal state."""

    def __init__(self, report: PoolTeardownReport) -> None:
        super().__init__(SAFE_POOL_TEARDOWN_ERROR_MESSAGE)
        self.report = report


def pool_worker_processes(pool: Any) -> tuple[Any, ...]:
    """Return the pool workers from the one deliberately isolated private seam."""

    return tuple(pool._pool)


@dataclass(slots=True)
class _MutableReport:
    reason: PoolTeardownReason
    worker_count: int | None = None
    alive_before_terminate: int | None = None
    alive_after_terminate: int | None = None
    alive_after_kill: int | None = None
    terminate_call_timed_out: bool = False
    terminate_reap_timed_out: bool = False
    kill_reap_timed_out: bool = False
    kill_requested: int = 0
    diagnostics: list[str] = field(default_factory=list)
    uncertain: bool = False

    def freeze(self, *, trustworthy: bool | None = None) -> PoolTeardownReport:
        return PoolTeardownReport(
            reason=self.reason,
            worker_count=self.worker_count,
            alive_before_terminate=self.alive_before_terminate,
            alive_after_terminate=self.alive_after_terminate,
            alive_after_kill=self.alive_after_kill,
            terminate_call_timed_out=self.terminate_call_timed_out,
            terminate_reap_timed_out=self.terminate_reap_timed_out,
            kill_reap_timed_out=self.kill_reap_timed_out,
            kill_requested=self.kill_requested,
            cleanup_diagnostics=tuple(self.diagnostics),
            cleanup_trustworthy=(not self.uncertain if trustworthy is None else trustworthy),
        )


@dataclass(frozen=True, slots=True)
class _CallResult:
    completed: bool
    timed_out: bool
    exception_type: str | None = None
    control_flow_error: BaseException | None = None


def _diagnose(
    report: _MutableReport,
    *,
    operation: str,
    reason: PoolTeardownReason,
    stage: str,
    exception: BaseException | None = None,
    timed_out: bool = False,
) -> None:
    """Record a diagnostic without ever retaining exception or input contents."""

    parts = [
        f"operation={operation}",
        f"reason={reason.value}",
        f"stage={stage}",
        f"workers={report.worker_count if report.worker_count is not None else 'unknown'}",
    ]
    if exception is not None:
        parts.append(f"exception_type={type(exception).__name__}")
    if timed_out:
        parts.append("timeout=true")
    message = " ".join(parts)
    report.diagnostics.append(message)
    _LOGGER.warning("%s", message)


def _bounded_call(
    operation: Callable[[], None],
    *,
    timeout_seconds: float,
    watcher_join_timeout_seconds: float,
) -> _CallResult:
    """Run one potentially blocking operation in a daemon watcher thread."""

    done = threading.Event()
    failure: list[Exception] = []
    control_flow_failure: list[BaseException] = []

    def invoke() -> None:
        try:
            operation()
        except Exception as error:
            failure.append(error)
        except BaseException as error:
            # Control-flow throwables must cross back to the owner thread; they
            # are never ordinary cleanup diagnostics.
            control_flow_failure.append(error)
        finally:
            done.set()

    watcher = threading.Thread(target=invoke, daemon=True)
    try:
        watcher.start()
    except Exception as error:
        return _CallResult(
            completed=False,
            timed_out=False,
            exception_type=type(error).__name__,
        )
    if not done.wait(max(0.0, timeout_seconds)):
        return _CallResult(completed=False, timed_out=True)

    # The operation has reported completion, so this join cannot make cleanup
    # unbounded. It only releases the short-lived watcher promptly.
    watcher.join(max(0.0, watcher_join_timeout_seconds))
    if control_flow_failure:
        return _CallResult(
            completed=True,
            timed_out=False,
            control_flow_error=control_flow_failure[0],
        )
    if failure:
        return _CallResult(
            completed=True,
            timed_out=False,
            exception_type=type(failure[0]).__name__,
        )
    return _CallResult(completed=True, timed_out=False)


def _remaining(deadline: float, monotonic: Callable[[], float]) -> float:
    return max(0.0, deadline - monotonic())


def _alive_workers(
    workers: Iterable[Any],
    *,
    report: _MutableReport,
    reason: PoolTeardownReason,
    stage: str,
) -> tuple[list[Any], bool]:
    alive: list[Any] = []
    unknown = False
    for worker in workers:
        try:
            if worker.is_alive():
                alive.append(worker)
        except Exception as error:
            unknown = True
            report.uncertain = True
            _diagnose(
                report,
                operation="worker.is_alive",
                reason=reason,
                stage=stage,
                exception=error,
            )
    return alive, unknown


def _merge_worker_handles(
    known_workers: Iterable[Any],
    observed_workers: Iterable[Any],
) -> tuple[Any, ...]:
    """Keep every process handle seen across a pool's mutable worker list."""

    merged = list(known_workers)
    known_ids = {id(worker) for worker in merged}
    for worker in observed_workers:
        if id(worker) not in known_ids:
            merged.append(worker)
            known_ids.add(id(worker))
    return tuple(merged)


def _call_worker_operation(
    worker: Any,
    operation_name: str,
    *,
    deadline: float,
    policy: PoolTeardownPolicy,
    monotonic: Callable[[], float],
    report: _MutableReport,
    reason: PoolTeardownReason,
    stage: str,
) -> bool:
    operation = getattr(worker, operation_name, None)
    if not callable(operation):
        _diagnose(
            report,
            operation=f"worker.{operation_name}",
            reason=reason,
            stage=stage,
        )
        return False

    result = _bounded_call(
        operation,
        timeout_seconds=_remaining(deadline, monotonic),
        watcher_join_timeout_seconds=policy.watcher_join_timeout_seconds,
    )
    if result.control_flow_error is not None:
        raise result.control_flow_error
    if result.timed_out:
        _diagnose(
            report,
            operation=f"worker.{operation_name}",
            reason=reason,
            stage=stage,
            timed_out=True,
        )
        return False
    if result.exception_type is not None:
        # The exception instance stayed inside the watcher. Its type is enough
        # for lifecycle diagnostics and avoids leaking source paths or payloads.
        report.diagnostics.append(
            " ".join(
                (
                    f"operation=worker.{operation_name}",
                    f"reason={reason.value}",
                    f"stage={stage}",
                    f"workers={report.worker_count if report.worker_count is not None else 'unknown'}",
                    f"exception_type={result.exception_type}",
                )
            )
        )
        _LOGGER.warning("%s", report.diagnostics[-1])
        return False
    return True


def _reap_workers(
    workers: tuple[Any, ...],
    *,
    deadline: float,
    policy: PoolTeardownPolicy,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
    report: _MutableReport,
    reason: PoolTeardownReason,
    stage: str,
) -> tuple[int | None, bool]:
    """Join all known live workers, bounded by one deadline for the whole phase."""

    unknown_seen = False
    while True:
        alive, unknown = _alive_workers(
            workers, report=report, reason=reason, stage=f"{stage}_state"
        )
        unknown_seen = unknown_seen or unknown
        if not alive:
            return (None if unknown_seen else 0), False

        remaining = _remaining(deadline, monotonic)
        if remaining <= 0.0:
            return len(alive), True

        for worker in alive:
            if _remaining(deadline, monotonic) <= 0.0:
                return len(alive), True
            _call_worker_operation(
                worker,
                "join",
                deadline=deadline,
                policy=policy,
                monotonic=monotonic,
                report=report,
                reason=reason,
                stage=stage,
            )

        remaining = _remaining(deadline, monotonic)
        if remaining <= 0.0:
            alive, unknown = _alive_workers(
                workers, report=report, reason=reason, stage=f"{stage}_state"
            )
            unknown_seen = unknown_seen or unknown
            return (None if unknown_seen else len(alive)), bool(alive)
        sleep(min(max(0.0, policy.poll_interval_seconds), remaining))


def _close_verified_dead_workers(
    workers: tuple[Any, ...],
    *,
    deadline: float,
    policy: PoolTeardownPolicy,
    monotonic: Callable[[], float],
    report: _MutableReport,
    reason: PoolTeardownReason,
) -> None:
    """Best-effort ``Process.close`` only after observing a dead worker."""

    for worker in workers:
        try:
            dead = not worker.is_alive()
        except Exception as error:
            report.uncertain = True
            _diagnose(
                report,
                operation="worker.is_alive",
                reason=reason,
                stage="close_state",
                exception=error,
            )
            continue
        if dead:
            # ``multiprocessing.Process.close`` invalidates the public Process
            # object. Callers commonly retain those objects to verify cleanup,
            # so leave standard-library multiprocessing handles inspectable.
            # Other process-like adapters still receive best-effort close.
            if type(worker).__module__.startswith("multiprocessing"):
                continue
            _call_worker_operation(
                worker,
                "close",
                deadline=deadline,
                policy=policy,
                monotonic=monotonic,
                report=report,
                reason=reason,
                stage="close",
            )


def _force_cleanup(
    pool: Any,
    *,
    reason: PoolTeardownReason,
    policy: PoolTeardownPolicy,
    worker_adapter: Callable[[Any], Iterable[Any]],
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
) -> PoolTeardownReport:
    report = _MutableReport(reason=reason)
    try:
        workers = tuple(worker_adapter(pool))
        report.worker_count = len(workers)
    except Exception as error:
        workers = None
        _diagnose(
            report,
            operation="worker_adapter",
            reason=reason,
            stage="capture",
            exception=error,
        )

    pool_terminate = _bounded_call(
        pool.terminate,
        timeout_seconds=max(0.0, policy.terminate_call_timeout_seconds),
        watcher_join_timeout_seconds=policy.watcher_join_timeout_seconds,
    )
    if pool_terminate.control_flow_error is not None:
        raise pool_terminate.control_flow_error
    report.terminate_call_timed_out = pool_terminate.timed_out
    if pool_terminate.timed_out:
        # A timed-out Pool.terminate may leave its worker-handler/finalizer
        # thread active.  That thread can replace workers after the initial
        # snapshot, so even a currently empty/dead inventory is not proof of a
        # stable terminal state.  Direct escalation still runs below, but the
        # outcome must fail closed.
        report.uncertain = True
        _diagnose(
            report,
            operation="pool.terminate",
            reason=reason,
            stage="terminate_call",
            timed_out=True,
        )
    elif pool_terminate.exception_type is not None:
        # A raised Pool.terminate can leave CPython's worker handler/finalizer
        # active just like a timeout.  It may still mutate the private worker
        # list after the first snapshot, so a dead captured inventory alone is
        # not enough to prove a trustworthy terminal state.
        report.uncertain = True
        report.diagnostics.append(
            " ".join(
                (
                    "operation=pool.terminate",
                    f"reason={reason.value}",
                    "stage=terminate_call",
                    f"workers={report.worker_count if report.worker_count is not None else 'unknown'}",
                    f"exception_type={pool_terminate.exception_type}",
                )
            )
        )
        _LOGGER.warning("%s", report.diagnostics[-1])

    if workers is None:
        # A completed terminate is a sufficient terminal state when no worker
        # inventory can be obtained. An exception or timeout leaves no proof.
        report.uncertain = pool_terminate.timed_out or pool_terminate.exception_type is not None
        return report.freeze()

    # Pool._pool is mutable while the worker handler is winding down. Keep the
    # initial handles, but refresh the adapter after pool termination so a
    # replacement created during that race cannot escape escalation.
    worker_inventory = workers

    def refresh_worker_inventory(stage: str) -> bool:
        nonlocal worker_inventory
        try:
            observed_workers = tuple(worker_adapter(pool))
        except Exception as error:
            # The initial inventory remains usable for best-effort escalation,
            # but a later failed observation means final state is not proven.
            report.uncertain = True
            _diagnose(
                report,
                operation="worker_adapter",
                reason=reason,
                stage=stage,
                exception=error,
            )
            return False
        worker_inventory = _merge_worker_handles(worker_inventory, observed_workers)
        report.worker_count = len(worker_inventory)
        return True

    refresh_worker_inventory("after_pool_terminate")

    alive, unknown = _alive_workers(
        worker_inventory, report=report, reason=reason, stage="before_terminate"
    )
    report.alive_before_terminate = None if unknown else len(alive)

    terminate_deadline = monotonic() + max(0.0, policy.terminate_reap_timeout_seconds)
    for worker in alive:
        if _remaining(terminate_deadline, monotonic) <= 0.0:
            report.terminate_reap_timed_out = True
            break
        _call_worker_operation(
            worker,
            "terminate",
            deadline=terminate_deadline,
            policy=policy,
            monotonic=monotonic,
            report=report,
            reason=reason,
            stage="worker_terminate",
        )

    report.alive_after_terminate, report.terminate_reap_timed_out = _reap_workers(
        worker_inventory,
        deadline=terminate_deadline,
        policy=policy,
        monotonic=monotonic,
        sleep=sleep,
        report=report,
        reason=reason,
        stage="terminate_reap",
    )

    refresh_worker_inventory("before_kill")
    survivors, unknown = _alive_workers(
        worker_inventory, report=report, reason=reason, stage="before_kill"
    )
    kill_deadline = monotonic() + max(0.0, policy.kill_reap_timeout_seconds)
    for worker in survivors:
        kill = getattr(worker, "kill", None)
        if not callable(kill):
            report.uncertain = True
            _diagnose(
                report,
                operation="worker.kill",
                reason=reason,
                stage="kill",
            )
            continue
        if _remaining(kill_deadline, monotonic) <= 0.0:
            report.kill_reap_timed_out = True
            break
        report.kill_requested += 1
        _call_worker_operation(
            worker,
            "kill",
            deadline=kill_deadline,
            policy=policy,
            monotonic=monotonic,
            report=report,
            reason=reason,
            stage="kill",
        )

    report.alive_after_kill, report.kill_reap_timed_out = _reap_workers(
        worker_inventory,
        deadline=kill_deadline,
        policy=policy,
        monotonic=monotonic,
        sleep=sleep,
        report=report,
        reason=reason,
        stage="kill_reap",
    )

    # Re-read the current pool inventory after escalation. If a replacement
    # appeared after the earlier refresh, kill and reap it against the same
    # global kill deadline rather than returning a false clean result.
    known_worker_ids = {id(worker) for worker in worker_inventory}
    refresh_worker_inventory("final_inventory")
    replacement_workers = tuple(
        worker
        for worker in worker_inventory
        if id(worker) not in known_worker_ids
    )
    replacement_survivors, replacement_unknown = _alive_workers(
        replacement_workers,
        report=report,
        reason=reason,
        stage="final_inventory_state",
    )
    if replacement_unknown:
        report.uncertain = True
    if replacement_survivors:
        if _remaining(kill_deadline, monotonic) <= 0.0:
            report.kill_reap_timed_out = True
        else:
            for worker in replacement_survivors:
                if _remaining(kill_deadline, monotonic) <= 0.0:
                    report.kill_reap_timed_out = True
                    break
                kill = getattr(worker, "kill", None)
                if not callable(kill):
                    report.uncertain = True
                    _diagnose(
                        report,
                        operation="worker.kill",
                        reason=reason,
                        stage="final_inventory_kill",
                    )
                    continue
                report.kill_requested += 1
                _call_worker_operation(
                    worker,
                    "kill",
                    deadline=kill_deadline,
                    policy=policy,
                    monotonic=monotonic,
                    report=report,
                    reason=reason,
                    stage="final_inventory_kill",
                )
            _reap_workers(
                worker_inventory,
                deadline=kill_deadline,
                policy=policy,
                monotonic=monotonic,
                sleep=sleep,
                report=report,
                reason=reason,
                stage="final_inventory_reap",
            )

    # Give the mutable pool inventory one bounded stability observation. This
    # catches a replacement that appears just after the first final refresh;
    # any newly observed live handle is escalated against the same global kill
    # deadline before we decide whether cleanup is trustworthy.
    previous_inventory_ids = tuple(id(worker) for worker in worker_inventory)
    inventory_stable = False
    while _remaining(kill_deadline, monotonic) > 0.0:
        sleep(
            min(
                max(0.0, policy.poll_interval_seconds),
                _remaining(kill_deadline, monotonic),
            )
        )
        known_worker_ids = set(previous_inventory_ids)
        if not refresh_worker_inventory("final_inventory_stability"):
            break
        current_inventory_ids = tuple(id(worker) for worker in worker_inventory)
        if current_inventory_ids == previous_inventory_ids:
            inventory_stable = True
            break
        new_workers = tuple(
            worker
            for worker in worker_inventory
            if id(worker) not in known_worker_ids
        )
        new_survivors, new_unknown = _alive_workers(
            new_workers,
            report=report,
            reason=reason,
            stage="final_inventory_stability_state",
        )
        if new_unknown:
            report.uncertain = True
        for worker in new_survivors:
            if _remaining(kill_deadline, monotonic) <= 0.0:
                report.kill_reap_timed_out = True
                break
            kill = getattr(worker, "kill", None)
            if not callable(kill):
                report.uncertain = True
                _diagnose(
                    report,
                    operation="worker.kill",
                    reason=reason,
                    stage="final_inventory_stability_kill",
                )
                continue
            report.kill_requested += 1
            _call_worker_operation(
                worker,
                "kill",
                deadline=kill_deadline,
                policy=policy,
                monotonic=monotonic,
                report=report,
                reason=reason,
                stage="final_inventory_stability_kill",
            )
        if new_survivors:
            _reap_workers(
                worker_inventory,
                deadline=kill_deadline,
                policy=policy,
                monotonic=monotonic,
                sleep=sleep,
                report=report,
                reason=reason,
                stage="final_inventory_stability_reap",
            )
        previous_inventory_ids = current_inventory_ids
    if not inventory_stable:
        report.uncertain = True

    final_alive, final_unknown = _alive_workers(
        worker_inventory,
        report=report,
        reason=reason,
        stage="final_state",
    )
    report.alive_after_kill = None if final_unknown else len(final_alive)

    if report.alive_after_kill not in (0, None):
        report.uncertain = True
    if report.alive_after_kill is None:
        report.uncertain = True
    _close_verified_dead_workers(
        worker_inventory,
        deadline=kill_deadline,
        policy=policy,
        monotonic=monotonic,
        report=report,
        reason=reason,
    )
    return report.freeze()


def teardown_worker_pool(
    pool: Any,
    *,
    reason: PoolTeardownReason,
    policy: PoolTeardownPolicy = DEFAULT_POOL_TEARDOWN_POLICY,
    worker_adapter: Callable[[Any], Iterable[Any]] = pool_worker_processes,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> PoolTeardownReport:
    """Release an extraction pool without allowing cleanup to block indefinitely."""

    reason = PoolTeardownReason(reason)
    try:
        if reason is PoolTeardownReason.SUCCESS:
            report = _MutableReport(reason=reason)
            graceful_failed = False
            try:
                pool.close()
            except Exception as error:
                graceful_failed = True
                _diagnose(
                    report,
                    operation="pool.close",
                    reason=reason,
                    stage="graceful",
                    exception=error,
                )

            # A failed close means the pool is no longer on the normal
            # lifecycle path. Never attempt the unbounded join in that case;
            # go straight to the bounded fallback below.
            if not graceful_failed:
                try:
                    pool.join()
                except Exception as error:
                    graceful_failed = True
                    _diagnose(
                        report,
                        operation="pool.join",
                        reason=reason,
                        stage="graceful",
                        exception=error,
                    )
            if not graceful_failed:
                return report.freeze()

            fallback = _force_cleanup(
                pool,
                reason=reason,
                policy=policy,
                worker_adapter=worker_adapter,
                monotonic=monotonic,
                sleep=sleep,
            )
            failed_report = replace(
                fallback,
                cleanup_diagnostics=tuple(report.diagnostics) + fallback.cleanup_diagnostics,
                cleanup_trustworthy=False,
            )
            raise WorkerPoolTeardownError(failed_report)

        report = _force_cleanup(
            pool,
            reason=reason,
            policy=policy,
            worker_adapter=worker_adapter,
            monotonic=monotonic,
            sleep=sleep,
        )
        if not report.cleanup_trustworthy:
            raise WorkerPoolTeardownError(report)
        return report
    except WorkerPoolTeardownError:
        raise
    except Exception as error:
        # Internal lifecycle failures (for example, inability to start the
        # daemon watcher) must have the same safe shape as ordinary cleanup
        # uncertainty so callers can preserve an active primary exception.
        report = _MutableReport(reason=reason)
        _diagnose(
            report,
            operation="pool_teardown",
            reason=reason,
            stage="internal",
            exception=error,
        )
        raise WorkerPoolTeardownError(report.freeze(trustworthy=False)) from None
