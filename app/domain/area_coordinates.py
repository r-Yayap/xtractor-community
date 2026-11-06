from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Sequence


Rect = tuple[float, float, float, float]




def _positive_dimension(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("Coordinate dimensions must be finite and positive.")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "Coordinate dimensions must be finite and positive."
        ) from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError("Coordinate dimensions must be finite and positive.")
    return number


def _rect(value: Sequence[float]) -> Rect:
    if len(value) != 4:
        raise ValueError("A rectangle must contain four coordinates.")
    rect = tuple(float(item) for item in value)
    if not all(math.isfinite(item) for item in rect):
        raise ValueError("Rectangle coordinates must be finite.")
    return rect  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class CoordinateBasis:
    width: float
    height: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "width", _positive_dimension(self.width))
        object.__setattr__(self, "height", _positive_dimension(self.height))


@dataclass(frozen=True, slots=True)
class CoordinateTransform:
    source: CoordinateBasis
    target: CoordinateBasis

    @property
    def x_scale(self) -> float:
        return self.target.width / self.source.width

    @property
    def y_scale(self) -> float:
        return self.target.height / self.source.height

    def to_target_rect(self, rect: Sequence[float]) -> Rect:
        x0, y0, x1, y1 = _rect(rect)
        return (
            x0 * self.x_scale,
            y0 * self.y_scale,
            x1 * self.x_scale,
            y1 * self.y_scale,
        )

    def to_source_rect(self, rect: Sequence[float]) -> Rect:
        x0, y0, x1, y1 = _rect(rect)
        return (
            x0 / self.x_scale,
            y0 / self.y_scale,
            x1 / self.x_scale,
            y1 / self.y_scale,
        )
