from __future__ import annotations

import os
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class PdfTreeNode:
    name: str
    path: Path | None = None
    children: list["PdfTreeNode"] = field(default_factory=list)
    selected: bool = False
    total_pdf_count: int = 0
    selected_pdf_count: int = 0


@dataclass(slots=True)
class PdfScanResult:
    roots: list[PdfTreeNode] = field(default_factory=list)
    selected_paths: dict[str, Path] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    cancelled: bool = False

    @property
    def total_pdf_count(self) -> int:
        return sum(root.total_pdf_count for root in self.roots)


@dataclass(frozen=True, slots=True)
class PdfScanBranch:
    branch_id: str
    name: str
    path: Path
    parent_labels: tuple[str, ...]
    is_file: bool


@dataclass(slots=True)
class PdfScanManifest:
    root_name: str | None = None
    branches: list[PdfScanBranch] = field(default_factory=list)
    empty_groups: list[tuple[str, ...]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class _ScanCancelled(Exception):
    pass


def canonical_pdf_path(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _is_directory_link(path: str | os.PathLike[str]) -> bool:
    if os.path.islink(path):
        return True
    is_junction = getattr(os.path, "isjunction", None)
    return bool(is_junction and is_junction(path))


def _is_descendant_path(path: Path, possible_parent: Path) -> bool:
    try:
        return os.path.commonpath((canonical_pdf_path(path), canonical_pdf_path(possible_parent))) == canonical_pdf_path(possible_parent)
    except ValueError:
        return False


def _normalized_manifest_sources(
    sources: Sequence[str | os.PathLike[str]],
) -> list[Path]:
    unique: list[Path] = []
    seen: set[str] = set()
    for source in sources:
        path = Path(os.path.abspath(os.fspath(source)))
        canonical = canonical_pdf_path(path)
        if canonical not in seen:
            seen.add(canonical)
            unique.append(path)

    directory_sources = [
        path
        for path in unique
        if not _is_directory_link(path) and path.is_dir()
    ]
    return [
        path
        for path in unique
        if not any(
            path != parent and _is_descendant_path(path, parent)
            for parent in directory_sources
        )
    ]


def build_pdf_scan_manifest(
    sources: Sequence[str | os.PathLike[str]],
    *,
    synthetic_root_name: str | None = None,
) -> PdfScanManifest:
    manifest = PdfScanManifest(root_name=synthetic_root_name)

    for source_path in _normalized_manifest_sources(sources):
        if _is_directory_link(source_path):
            continue
        if source_path.is_file():
            if source_path.name.lower().endswith(".pdf"):
                manifest.branches.append(
                    PdfScanBranch(
                        branch_id=canonical_pdf_path(source_path),
                        name=source_path.name,
                        path=source_path,
                        parent_labels=(),
                        is_file=True,
                    )
                )
            continue
        if not source_path.is_dir():
            manifest.warnings.append(f"{source_path}: source is not available")
            continue

        parent_labels = (source_path.name or str(source_path),)
        try:
            with os.scandir(source_path) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name.casefold())
        except OSError as exc:
            manifest.warnings.append(f"{source_path}: {exc}")
            continue

        source_branch_count = 0
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                is_junction = getattr(entry, "is_junction", None)
                if is_junction is not None and is_junction():
                    continue
                is_directory = entry.is_dir(follow_symlinks=False)
                is_pdf = entry.is_file(follow_symlinks=False) and entry.name.lower().endswith(".pdf")
            except OSError as exc:
                manifest.warnings.append(f"{entry.path}: {exc}")
                continue
            if not is_directory and not is_pdf:
                continue
            entry_path = Path(os.path.abspath(entry.path))
            manifest.branches.append(
                PdfScanBranch(
                    branch_id=canonical_pdf_path(entry_path),
                    name=entry.name,
                    path=entry_path,
                    parent_labels=parent_labels,
                    is_file=is_pdf,
                )
            )
            source_branch_count += 1

        if source_branch_count == 0:
            manifest.empty_groups.append(parent_labels)

    return manifest


def scan_pdf_sources(
    sources: Sequence[str | os.PathLike[str]],
    *,
    synthetic_root_name: str | None = None,
    cancel_event: threading.Event | None = None,
    progress: Callable[[int], None] | None = None,
) -> PdfScanResult:
    warnings: list[str] = []
    selected_paths: dict[str, Path] = {}
    seen_paths: set[str] = set()
    found_count = 0

    def check_cancelled() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise _ScanCancelled

    def record_pdf(path: str | os.PathLike[str], name: str) -> PdfTreeNode | None:
        nonlocal found_count
        original = Path(os.path.abspath(os.fspath(path)))
        canonical = canonical_pdf_path(original)
        if canonical in seen_paths:
            return None
        seen_paths.add(canonical)
        selected_paths[canonical] = original
        found_count += 1
        if progress is not None:
            progress(found_count)
        return PdfTreeNode(
            name=name,
            path=original,
            selected=True,
            total_pdf_count=1,
            selected_pdf_count=1,
        )

    def scan_directory(directory: Path) -> PdfTreeNode | None:
        check_cancelled()
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name.casefold())
        except OSError as exc:
            warnings.append(f"{directory}: {exc}")
            return None

        children: list[PdfTreeNode] = []
        for entry in entries:
            check_cancelled()
            try:
                if entry.is_symlink():
                    continue
                is_junction = getattr(entry, "is_junction", None)
                if is_junction is not None and is_junction():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    child = scan_directory(Path(entry.path))
                    if child is not None:
                        children.append(child)
                elif entry.is_file(follow_symlinks=False) and entry.name.lower().endswith(".pdf"):
                    child = record_pdf(entry.path, entry.name)
                    if child is not None:
                        children.append(child)
            except OSError as exc:
                warnings.append(f"{entry.path}: {exc}")

        total = sum(child.total_pdf_count for child in children)
        if total == 0:
            return None
        selected = sum(child.selected_pdf_count for child in children)
        return PdfTreeNode(
            name=directory.name or str(directory),
            children=children,
            selected=selected == total,
            total_pdf_count=total,
            selected_pdf_count=selected,
        )

    roots: list[PdfTreeNode] = []
    try:
        for source in sources:
            check_cancelled()
            source_path = Path(os.path.abspath(os.fspath(source)))
            if _is_directory_link(source_path):
                continue
            if source_path.is_dir():
                node = scan_directory(source_path)
            elif source_path.is_file() and source_path.name.lower().endswith(".pdf"):
                node = record_pdf(source_path, source_path.name)
            else:
                node = None
            if node is not None:
                roots.append(node)
    except _ScanCancelled:
        return PdfScanResult(warnings=warnings, cancelled=True)

    if synthetic_root_name and roots:
        total = sum(root.total_pdf_count for root in roots)
        selected = sum(root.selected_pdf_count for root in roots)
        roots = [
            PdfTreeNode(
                name=synthetic_root_name,
                children=roots,
                selected=selected == total,
                total_pdf_count=total,
                selected_pdf_count=selected,
            )
        ]

    return PdfScanResult(
        roots=roots,
        selected_paths=selected_paths,
        warnings=warnings,
    )
