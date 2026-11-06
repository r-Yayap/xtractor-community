from __future__ import annotations

import threading
import time
from pathlib import Path

from app.services.pdf_selection_service import (
    PdfScanBranch,
    PdfScanManifest,
    PdfScanResult,
    PdfTreeNode,
    canonical_pdf_path,
)


class PdfSelectionCoordinator:
    """Own the state transitions for one asynchronous PDF scan session."""

    def __init__(self) -> None:
        self.request_id = 0
        self.selection_generation = 0
        self.cancel_event: threading.Event | None = None
        self.selected_paths: dict[str, Path] = {}
        self.branches: dict[str, PdfScanBranch] = {}
        self.branch_states: dict[str, str] = {}
        self.branch_counts: dict[str, int] = {}
        self.branch_started: dict[str, float] = {}
        self.pending_branch_ids: set[str] = set()
        self.cancelled = False
        self.had_failure = False
        self.active = False
        self.is_drop = False
        self.preview_first = False
        self.preview_path: str | None = None
        self.warnings: list[str] = []

    def start(
        self,
        *,
        is_drop: bool,
        preview_first: bool,
        cancel_event: threading.Event | None = None,
    ) -> int:
        if self.cancel_event is not None:
            self.cancel_event.set()
        self.request_id += 1
        self.selection_generation += 1
        self.cancel_event = cancel_event or threading.Event()
        self.selected_paths.clear()
        self.branches.clear()
        self.branch_states.clear()
        self.branch_counts.clear()
        self.branch_started.clear()
        self.pending_branch_ids.clear()
        self.cancelled = False
        self.had_failure = False
        self.active = True
        self.is_drop = is_drop
        self.preview_first = preview_first
        self.preview_path = None
        self.warnings.clear()
        return self.request_id

    def is_current(self, request_id: int) -> bool:
        return request_id == self.request_id

    def accept_manifest(
        self,
        request_id: int,
        manifest: PdfScanManifest,
        *,
        now: float | None = None,
    ) -> bool:
        if not self.is_current(request_id) or not self.active:
            return False
        started = time.monotonic() if now is None else now
        self.warnings.extend(manifest.warnings)
        for branch in manifest.branches:
            self.branches[branch.branch_id] = branch
            self.branch_states[branch.branch_id] = "pending"
            self.branch_counts[branch.branch_id] = 0
            self.branch_started[branch.branch_id] = started
            self.pending_branch_ids.add(branch.branch_id)
        return True

    def update_branch_progress(
        self,
        request_id: int,
        branch_id: str,
        found_count: int,
    ) -> bool:
        if not self._accepts_branch_update(request_id, branch_id):
            return False
        self.branch_counts[branch_id] = found_count
        return True

    def accept_branch_result(
        self,
        request_id: int,
        branch_id: str,
        result: PdfScanResult,
    ) -> str | None:
        if not self._accepts_branch_update(request_id, branch_id):
            return None
        if result.cancelled:
            self._set_terminal(branch_id, "cancelled")
            return "cancelled"

        self.pending_branch_ids.discard(branch_id)
        self.selected_paths.update(result.selected_paths)
        self.warnings.extend(result.warnings)
        self.branch_counts[branch_id] = result.total_pdf_count
        state = "complete" if result.roots else "empty"
        self.branch_states[branch_id] = state
        if self.preview_first and self.preview_path is None:
            self.preview_path = self.first_selected_node_path(result.roots)
        return state

    def fail_branch(self, request_id: int, branch_id: str) -> bool:
        if not self._accepts_branch_update(request_id, branch_id):
            return False
        self.had_failure = True
        self._set_terminal(branch_id, "failed")
        return True

    def manifest_failed(self, request_id: int) -> bool:
        if not self.is_current(request_id) or not self.active:
            return False
        self.active = False
        return True

    def mark_stalled(
        self,
        *,
        now: float | None = None,
        threshold_seconds: float = 5.0,
    ) -> tuple[str, ...]:
        current = time.monotonic() if now is None else now
        stalled: list[str] = []
        for branch_id in tuple(self.pending_branch_ids):
            if self.branch_states.get(branch_id) != "pending":
                continue
            if current - self.branch_started.get(branch_id, current) < threshold_seconds:
                continue
            self.branch_states[branch_id] = "stalled"
            stalled.append(branch_id)
        return tuple(stalled)

    def cancel(self) -> tuple[str, ...]:
        if not self.active:
            return ()
        self.cancelled = True
        if self.cancel_event is not None:
            self.cancel_event.set()
        cancelled = tuple(self.pending_branch_ids)
        for branch_id in cancelled:
            self.branch_states[branch_id] = "cancelled"
        self.pending_branch_ids.clear()
        self.active = False
        return cancelled

    def finish_if_ready(self, *, tree_pending: bool) -> bool:
        if self.pending_branch_ids or tree_pending:
            return False
        self.active = False
        return True

    def set_selected_path(self, path: str | Path, selected: bool) -> None:
        value = Path(path)
        key = canonical_pdf_path(value)
        if selected:
            if key in self.selected_paths:
                return
            self.selected_paths[key] = value
        else:
            if self.selected_paths.pop(key, None) is None:
                return
        self.selection_generation += 1

    def replace_selected_paths(self, paths: dict[str, Path]) -> None:
        """Replace the selected set and advance only when its identity changes."""
        replacement = dict(paths)
        if set(replacement) == set(self.selected_paths):
            self.selected_paths = replacement
            return
        self.selected_paths = replacement
        self.selection_generation += 1

    def clear_warnings(self) -> None:
        self.warnings.clear()

    def paths_for_extraction(self) -> list[Path]:
        if self.pending_branch_ids:
            return []
        return sorted(self.selected_paths.values(), key=lambda path: str(path).casefold())

    def accept_scan_result(self, request_id: int, result: PdfScanResult) -> bool:
        """Accept a legacy whole-scan result during the incremental migration."""
        if not self.is_current(request_id):
            return False
        if result.cancelled:
            self.active = False
            return True
        self.selected_paths = dict(result.selected_paths)
        self.warnings = list(result.warnings)
        if self.preview_first:
            self.preview_path = self.first_selected_node_path(result.roots)
        return True

    @property
    def found_count(self) -> int:
        return sum(self.branch_counts.values())

    def _accepts_branch_update(self, request_id: int, branch_id: str) -> bool:
        return (
            self.is_current(request_id)
            and self.active
            and self.branch_states.get(branch_id) in {"pending", "stalled"}
        )

    def _set_terminal(self, branch_id: str, state: str) -> None:
        self.pending_branch_ids.discard(branch_id)
        self.branch_states[branch_id] = state

    @staticmethod
    def first_selected_node_path(roots: list[PdfTreeNode]) -> str | None:
        stack = list(reversed(roots))
        while stack:
            node = stack.pop()
            if node.path is not None and node.selected:
                return str(node.path)
            stack.extend(reversed(node.children))
        return None
