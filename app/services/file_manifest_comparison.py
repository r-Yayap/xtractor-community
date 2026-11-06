"""Pure current-batch and historical file identity classification."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Iterable, Sequence

from app.domain.file_manifest import (
    FileManifestEntry,
    has_authoritative_identity,
)


_HASH_UNAVAILABLE = "Hash unavailable"
_IDENTITY_UNAVAILABLE = "Identity unavailable"
_IDENTITY_UNRELIABLE = (
    "Identity unreliable — source changed during extraction"
)


@dataclass(frozen=True, slots=True)
class ManifestComparison:
    """A relationship result for one current or previous manifest entry."""

    entry: FileManifestEntry
    relationship: str
    previous_entry: FileManifestEntry | None = None
    current_entry: FileManifestEntry | None = None


def classify_current_batch(
    entries: Iterable[FileManifestEntry],
) -> list[FileManifestEntry]:
    """Classify only authoritative same-snapshot content relationships."""
    ordered = list(entries)
    groups: dict[str, list[tuple[int, FileManifestEntry]]] = defaultdict(list)
    for index, item in enumerate(ordered):
        if has_authoritative_identity(item):
            groups[_sha_key(item)].append((index, item))

    sorted_groups = sorted(
        groups.values(),
        key=lambda group: min(
            (_path_key(item.relative_path), item.file_id) for _, item in group
        ) + (_sha_key(group[0][1]),),
    )
    duplicate_groups = [group for group in sorted_groups if len(group) > 1]
    group_labels = {
        _sha_key(group[0][1]): f"DUP-{group_index:03d}"
        for group_index, group in enumerate(duplicate_groups, start=1)
    }

    result = [
        replace(
            item,
            content_group="",
            current_relationship=_unavailable_relationship(item),
            related_files=(),
        )
        for item in ordered
    ]

    for sha, group in groups.items():
        if len(group) == 1:
            index, item = group[0]
            result[index] = replace(
                item,
                content_group="Unique",
                current_relationship="Unique",
                related_files=(),
            )
            continue

        ordered_group = sorted(
            group,
            key=lambda pair: (_path_key(pair[1].relative_path), pair[1].file_id),
        )
        path_counts: dict[str, int] = defaultdict(int)
        for _, item in group:
            path_counts[_path_key(item.relative_path)] += 1
        related_by_index = {
            index: tuple(
                other.relative_path
                for other_index, other in ordered_group
                if other_index != index
            )
            for index, _ in group
        }

        for index, item in group:
            peers = [
                other
                for other_index, other in ordered_group
                if other_index != index
            ]
            if path_counts[_path_key(item.relative_path)] > 1:
                relationship = "Repeated selection"
            else:
                folder_differs = any(
                    _folder_key(peer.folder) != _folder_key(item.folder)
                    for peer in peers
                )
                filename_differs = any(
                    _name_key(peer.filename) != _name_key(item.filename)
                    for peer in peers
                )
                if folder_differs and filename_differs:
                    relationship = (
                        "Same content — different filename and folder"
                    )
                elif folder_differs:
                    relationship = "Same content — different folder"
                elif filename_differs:
                    relationship = "Same content — different filename"
                else:
                    relationship = (
                        "Same content — different filename and folder"
                    )
            result[index] = replace(
                item,
                content_group=group_labels[sha],
                current_relationship=relationship,
                related_files=related_by_index[index],
            )
    return result


def compare_manifests(
    previous: Sequence[FileManifestEntry],
    current: Sequence[FileManifestEntry],
) -> list[ManifestComparison]:
    """Compare snapshots without using unavailable or unstable SHA values."""
    previous_remaining = set(range(len(previous)))
    current_remaining = set(range(len(current)))
    results: list[ManifestComparison] = []

    # Exact path + authoritative same SHA is the strongest identity signal.
    for current_index in _ordered_indexes(current):
        if current_index not in current_remaining:
            continue
        candidates = [
            index
            for index in previous_remaining
            if _same_path(previous[index], current[current_index])
            and _same_authoritative_sha(previous[index], current[current_index])
        ]
        if not candidates:
            continue
        previous_index = min(
            candidates,
            key=lambda index: _identity_key(previous[index]),
        )
        previous_remaining.remove(previous_index)
        current_remaining.remove(current_index)
        results.append(
            ManifestComparison(
                entry=current[current_index],
                relationship="Unchanged",
                previous_entry=previous[previous_index],
                current_entry=current[current_index],
            )
        )

    # Resolve remaining same-path pairs before any move/rename inference.
    same_path_keys = sorted(
        {
            _path_key(previous[index].relative_path)
            for index in previous_remaining
        }
        & {
            _path_key(current[index].relative_path)
            for index in current_remaining
        }
    )
    for path_key in same_path_keys:
        previous_indexes = [
            index
            for index in previous_remaining
            if _path_key(previous[index].relative_path) == path_key
        ]
        current_indexes = [
            index
            for index in current_remaining
            if _path_key(current[index].relative_path) == path_key
        ]
        previous_indexes.sort(key=lambda index: _identity_key(previous[index]))
        current_indexes.sort(key=lambda index: _identity_key(current[index]))

        if len(previous_indexes) != 1 or len(current_indexes) != 1:
            for current_index in current_indexes:
                current_remaining.remove(current_index)
                results.append(
                    ManifestComparison(
                        entry=current[current_index],
                        relationship="Ambiguous duplicate provenance",
                        current_entry=current[current_index],
                    )
                )
            for previous_index in previous_indexes:
                previous_remaining.remove(previous_index)
                results.append(
                    ManifestComparison(
                        entry=previous[previous_index],
                        relationship="Ambiguous duplicate provenance",
                        previous_entry=previous[previous_index],
                    )
                )
            continue

        previous_index = previous_indexes[0]
        current_index = current_indexes[0]
        previous_remaining.remove(previous_index)
        current_remaining.remove(current_index)
        relationship = (
            "Modified"
            if _same_authoritative_path_with_different_sha(
                previous[previous_index],
                current[current_index],
            )
            else _IDENTITY_UNAVAILABLE
        )
        results.append(
            ManifestComparison(
                entry=current[current_index],
                relationship=relationship,
                previous_entry=previous[previous_index],
                current_entry=current[current_index],
            )
        )

    previous_by_sha: dict[str, list[int]] = defaultdict(list)
    current_by_sha: dict[str, list[int]] = defaultdict(list)
    for index in previous_remaining:
        if has_authoritative_identity(previous[index]):
            previous_by_sha[_sha_key(previous[index])].append(index)
    for index in current_remaining:
        if has_authoritative_identity(current[index]):
            current_by_sha[_sha_key(current[index])].append(index)

    for sha in sorted(set(previous_by_sha) | set(current_by_sha)):
        previous_indexes = sorted(
            previous_by_sha.get(sha, []),
            key=lambda index: _identity_key(previous[index]),
        )
        current_indexes = sorted(
            current_by_sha.get(sha, []),
            key=lambda index: _identity_key(current[index]),
        )
        if len(previous_indexes) == 1 and len(current_indexes) == 1:
            previous_index = previous_indexes[0]
            current_index = current_indexes[0]
            previous_remaining.remove(previous_index)
            current_remaining.remove(current_index)
            old = previous[previous_index]
            new = current[current_index]
            same_folder = _folder_key(old.folder) == _folder_key(new.folder)
            same_name = _name_key(old.filename) == _name_key(new.filename)
            relationship = (
                "Moved + Renamed"
                if not same_folder and not same_name
                else "Moved"
                if not same_folder
                else "Renamed"
                if not same_name
                else "Unchanged"
            )
            results.append(
                ManifestComparison(
                    entry=new,
                    relationship=relationship,
                    previous_entry=old,
                    current_entry=new,
                )
            )
            continue

        if current_indexes and previous_indexes:
            for current_index in current_indexes:
                current_remaining.remove(current_index)
                results.append(
                    ManifestComparison(
                        entry=current[current_index],
                        relationship="Ambiguous duplicate provenance",
                        current_entry=current[current_index],
                    )
                )
            for previous_index in previous_indexes:
                previous_remaining.remove(previous_index)
                results.append(
                    ManifestComparison(
                        entry=previous[previous_index],
                        relationship="Ambiguous duplicate provenance",
                        previous_entry=previous[previous_index],
                    )
                )
        elif current_indexes:
            relationship = (
                "New duplicate/copy"
                if any(
                    has_authoritative_identity(item)
                    and _sha_key(item) == sha
                    for item in previous
                )
                else "New"
            )
            for current_index in current_indexes:
                current_remaining.remove(current_index)
                results.append(
                    ManifestComparison(
                        entry=current[current_index],
                        relationship=relationship,
                        current_entry=current[current_index],
                    )
                )
        elif previous_indexes:
            for previous_index in previous_indexes:
                previous_remaining.remove(previous_index)
                results.append(
                    ManifestComparison(
                        entry=previous[previous_index],
                        relationship="Removed",
                        previous_entry=previous[previous_index],
                    )
                )

    for current_index in _ordered_indexes(current):
        if current_index in current_remaining:
            current_remaining.remove(current_index)
            results.append(
                ManifestComparison(
                    entry=current[current_index],
                    relationship=_IDENTITY_UNAVAILABLE
                    if not has_authoritative_identity(current[current_index])
                    else "New",
                    current_entry=current[current_index],
                )
            )
    for previous_index in _ordered_indexes(previous):
        if previous_index in previous_remaining:
            previous_remaining.remove(previous_index)
            results.append(
                ManifestComparison(
                    entry=previous[previous_index],
                    relationship=_IDENTITY_UNAVAILABLE
                    if not has_authoritative_identity(previous[previous_index])
                    else "Removed",
                    previous_entry=previous[previous_index],
                )
            )
    return sorted(results, key=lambda item: _identity_key(item.entry))


def _same_authoritative_path_with_different_sha(
    previous: FileManifestEntry,
    current: FileManifestEntry,
) -> bool:
    return (
        has_authoritative_identity(previous)
        and has_authoritative_identity(current)
        and _sha_key(previous) != _sha_key(current)
    )


def _same_authoritative_sha(
    previous: FileManifestEntry,
    current: FileManifestEntry,
) -> bool:
    return (
        has_authoritative_identity(previous)
        and has_authoritative_identity(current)
        and _sha_key(previous) == _sha_key(current)
    )


def _unavailable_relationship(entry: FileManifestEntry) -> str:
    return (
        _IDENTITY_UNRELIABLE
        if entry.source_changed_during_extraction
        else _HASH_UNAVAILABLE
    )


def _ordered_indexes(entries: Sequence[FileManifestEntry]) -> list[int]:
    return sorted(range(len(entries)), key=lambda index: _identity_key(entries[index]))


def _identity_key(entry: FileManifestEntry) -> tuple[str, str, str]:
    return (_path_key(entry.relative_path), entry.file_id, _sha_key(entry))


def _same_path(left: FileManifestEntry, right: FileManifestEntry) -> bool:
    return _path_key(left.relative_path) == _path_key(right.relative_path)


def _sha_key(entry: FileManifestEntry) -> str:
    return str(entry.sha256 or "").strip().lower()


def _path_key(value: str) -> str:
    return str(value).replace("\\", "/").casefold()


def _folder_key(value: str) -> str:
    return _path_key(value).rstrip("/")


def _name_key(value: str) -> str:
    return str(value).casefold()
