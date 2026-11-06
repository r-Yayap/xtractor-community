from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from PySide6 import QtGui

from app.domain.models import AreaSpec
from app.domain.area_coordinates import CoordinateBasis


EditKind = Literal[
    "add_area",
    "delete_area",
    "geometry",
    "replace_state",
]
Coordinates = tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class AreaSnapshot:
    editor_id: str
    area: AreaSpec

    @property
    def title(self) -> str:
        return self.area.title

    @property
    def coordinates(self) -> Coordinates:
        return self.area.rect

    @property
    def use_ocr(self) -> bool:
        return self.area.use_ocr

    @property
    def use_image(self) -> bool:
        return self.area.use_image

    @classmethod
    def from_area_spec(cls, area: AreaSpec, *, editor_id: str) -> "AreaSnapshot":
        return cls(editor_id=editor_id, area=area)


@dataclass(frozen=True, slots=True)
class ViewerStateSnapshot:
    areas: tuple[AreaSnapshot, ...] = ()
    coordinate_basis: CoordinateBasis | None = None


@dataclass(frozen=True, slots=True)
class ViewerEdit:
    kind: EditKind
    description: str
    target_id: str | None = None
    before: object = None
    after: object = None
    index: int | None = None

    @classmethod
    def geometry(
        cls,
        target_id: str,
        before: Coordinates,
        after: Coordinates,
        *,
        description: str,
    ) -> "ViewerEdit":
        return cls(
            kind="geometry",
            description=description,
            target_id=target_id,
            before=before,
            after=after,
        )

    @classmethod
    def add_area(cls, area: AreaSnapshot, index: int) -> "ViewerEdit":
        return cls(
            kind="add_area",
            description="Add area",
            target_id=area.editor_id,
            after=area,
            index=index,
        )

    @classmethod
    def delete_area(cls, area: AreaSnapshot, index: int) -> "ViewerEdit":
        return cls(
            kind="delete_area",
            description="Delete area",
            target_id=area.editor_id,
            before=area,
            index=index,
        )

    @classmethod
    def replace_all(
        cls,
        before: ViewerStateSnapshot,
        after: ViewerStateSnapshot,
    ) -> "ViewerEdit":
        return cls.replace_state(
            before,
            after,
            description="Remove all areas",
        )

    @classmethod
    def replace_state(
        cls,
        before: ViewerStateSnapshot,
        after: ViewerStateSnapshot,
        *,
        description: str,
    ) -> "ViewerEdit":
        return cls(
            kind="replace_state",
            description=description,
            before=before,
            after=after,
        )


class _ViewerEditCommand(QtGui.QUndoCommand):
    def __init__(
        self,
        edit: ViewerEdit,
        apply: Callable[[ViewerEdit, bool], None],
        *,
        already_applied: bool,
    ) -> None:
        super().__init__(edit.description)
        self._edit = edit
        self._apply = apply
        self._skip_first_redo = already_applied

    def undo(self) -> None:
        self._apply(self._edit, False)

    def redo(self) -> None:
        if self._skip_first_redo:
            self._skip_first_redo = False
            return
        self._apply(self._edit, True)


class ViewerEditHistory:
    """Bounded Qt undo stack for finalized viewer-level logical edits."""

    DEFAULT_LIMIT = 100

    def __init__(
        self,
        apply: Callable[[ViewerEdit, bool], None],
        *,
        limit: int = DEFAULT_LIMIT,
    ) -> None:
        if limit <= 0:
            raise ValueError("Viewer edit history limit must be positive")
        self._apply = apply
        self.stack = QtGui.QUndoStack()
        self.stack.setUndoLimit(limit)

    def execute(self, edit: ViewerEdit) -> None:
        self.stack.push(_ViewerEditCommand(edit, self._apply, already_applied=False))

    def record_applied(self, edit: ViewerEdit) -> None:
        self.stack.push(_ViewerEditCommand(edit, self._apply, already_applied=True))

    def clear(self) -> None:
        self.stack.clear()

    def undo(self) -> None:
        self.stack.undo()

    def redo(self) -> None:
        self.stack.redo()

    @property
    def count(self) -> int:
        return self.stack.count()

    @property
    def can_undo(self) -> bool:
        return self.stack.canUndo()

    @property
    def can_redo(self) -> bool:
        return self.stack.canRedo()
