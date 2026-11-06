from __future__ import annotations

import unicodedata

from app.infra.pdf.models import PdfCharacter, PdfRect, PdfWord
from app.infra.pdf.pdfium.coordinates import PdfiumCoordinateSpace
from app.infra.pdf.text_normalization import canonicalize_pdf_characters


def characters_in_rect(
    text_page,
    space: PdfiumCoordinateSpace,
    rect: PdfRect,
) -> tuple[PdfCharacter, ...]:
    positioned: list[tuple[str, PdfRect]] = []
    for index in range(text_page.count_chars()):
        value = text_page.get_text_range(index, 1, errors="replace")
        if not value:
            continue
        try:
            native_box = PdfRect.from_value(text_page.get_charbox(index))
        except Exception:
            continue
        box = space.pdfium_to_app(native_box)
        positioned.append((value, box))

    output: list[PdfCharacter] = []
    for index, (value, box) in enumerate(positioned):
        if _is_synthetic_separator(value) and not box.is_valid():
            box = _separator_gap_box(positioned, index)
        if box is None or box.intersect(rect) is None:
            continue
        output.append(PdfCharacter(box, value, (box.x0, box.y1)))
    return tuple(output)


def _is_synthetic_separator(value: str) -> bool:
    return bool(value) and all(
        character.isspace() for character in value
    ) and not any(character in "\r\n" for character in value)


def _separator_gap_box(
    positioned: list[tuple[str, PdfRect]],
    index: int,
) -> PdfRect | None:
    previous_match = _nearest_visible(positioned, index - 1, -1)
    following_match = _nearest_visible(positioned, index + 1, 1)
    if previous_match is None or following_match is None:
        return None
    previous_index, previous = previous_match
    following_index, following = following_match
    previous_neighbor = _nearest_visible(positioned, previous_index - 1, -1)
    following_neighbor = _nearest_visible(positioned, following_index + 1, 1)
    return _gap_between_visible_boxes(
        previous,
        following,
        previous_neighbor[1] if previous_neighbor is not None else None,
        following_neighbor[1] if following_neighbor is not None else None,
    )


def _nearest_visible(
    positioned: list[tuple[str, PdfRect]],
    start: int,
    step: int,
) -> tuple[int, PdfRect] | None:
    indices = (
        range(start, -1, -1)
        if step < 0
        else range(start, len(positioned))
    )
    for index in indices:
        value, box = positioned[index]
        if _is_line_break(value):
            return None
        if not _is_separator(value) and box.is_valid():
            return index, box
    return None


def _gap_between_visible_boxes(
    left: PdfRect,
    right: PdfRect,
    left_neighbor: PdfRect | None = None,
    right_neighbor: PdfRect | None = None,
) -> PdfRect | None:
    left = left.normalized()
    right = right.normalized()
    left_center_x = (left.x0 + left.x1) / 2
    left_center_y = (left.y0 + left.y1) / 2
    right_center_x = (right.x0 + right.x1) / 2
    right_center_y = (right.y0 + right.y1) / 2

    horizontal_support = _line_orientation_support(
        left,
        right,
        left_neighbor,
        right_neighbor,
        horizontal=True,
    )
    vertical_support = _line_orientation_support(
        left,
        right,
        left_neighbor,
        right_neighbor,
        horizontal=False,
    )
    use_horizontal = (
        horizontal_support > vertical_support
        if horizontal_support or vertical_support
        else abs(right_center_x - left_center_x)
        >= abs(right_center_y - left_center_y)
    )

    if use_horizontal:
        overlap_y0 = max(left.y0, right.y0)
        overlap_y1 = min(left.y1, right.y1)
        overlap = overlap_y1 - overlap_y0
        if overlap <= 0 or overlap / max(min(left.height, right.height), 1e-6) < 0.3:
            return None
        gap_x0 = min(left.x1, right.x1)
        gap_x1 = max(left.x0, right.x0)
        if gap_x1 <= gap_x0:
            return None
        return PdfRect(gap_x0, overlap_y0, gap_x1, overlap_y1)

    overlap_x0 = max(left.x0, right.x0)
    overlap_x1 = min(left.x1, right.x1)
    overlap = overlap_x1 - overlap_x0
    if overlap <= 0 or overlap / max(min(left.width, right.width), 1e-6) < 0.3:
        return None
    gap_y0 = min(left.y1, right.y1)
    gap_y1 = max(left.y0, right.y0)
    if gap_y1 <= gap_y0:
        return None
    return PdfRect(overlap_x0, gap_y0, overlap_x1, gap_y1)


def _line_orientation_support(
    left: PdfRect,
    right: PdfRect,
    left_neighbor: PdfRect | None,
    right_neighbor: PdfRect | None,
    *,
    horizontal: bool,
) -> float:
    if horizontal:
        axis_left = lambda rect: (rect.y0, rect.y1)
    else:
        axis_left = lambda rect: (rect.x0, rect.x1)

    support = 0.0
    for first, second in (
        (left_neighbor, left),
        (right, right_neighbor),
    ):
        if first is None or second is None:
            continue
        first_start, first_end = axis_left(first)
        second_start, second_end = axis_left(second)
        overlap = min(first_end, second_end) - max(first_start, second_start)
        minimum_size = max(
            min(first_end - first_start, second_end - second_start),
            1e-6,
        )
        ratio = overlap / minimum_size
        if ratio >= 0.3:
            support += ratio
    return support


def reconstruct_words(
    characters: tuple[PdfCharacter, ...],
    space: PdfiumCoordinateSpace,
) -> tuple[PdfWord, ...]:
    displayed = _reconstruct_words_with_geometry(
        characters,
        lambda character: character.rect,
    )
    if space.rotation_degrees not in {90, 270}:
        return displayed

    # Rotated PDFs occur in both forms: content authored upright and rotated
    # by page metadata, and content authored sideways with page rotation used
    # to make it upright.  Pick the orientation that forms the more coherent
    # word stream instead of assuming one authoring convention.
    native = _reconstruct_words_with_geometry(
        characters,
        lambda character: _unrotated_geometry(character.rect, space),
    )
    return displayed if len(displayed) <= len(native) else native


def reading_order_text(words: tuple[PdfWord, ...]) -> str:
    """Return positioned words in visual top-to-bottom, left-to-right order."""
    if not words:
        return ""

    prefix_index = _spanning_prefix_index(words)
    prefix = words[prefix_index] if prefix_index is not None else None
    row_words = (
        tuple(word for index, word in enumerate(words) if index != prefix_index)
        if prefix_index is not None
        else words
    )
    ordered_rows = _ordered_rows(row_words)
    if prefix is not None:
        first_bounds, first_row = ordered_rows[0]
        ordered_rows[0] = (
            _union((prefix.rect, first_bounds)),
            [prefix, *first_row],
        )

    return "\n".join(_row_text(row) for _bounds, row in ordered_rows)


def _ordered_rows(
    words: tuple[PdfWord, ...],
) -> list[tuple[PdfRect, list[PdfWord]]]:
    rows: list[list[PdfWord]] = []
    row_bounds: list[PdfRect] = []
    for word in sorted(words, key=lambda item: (item.rect.y0, item.rect.x0)):
        best_index: int | None = None
        best_overlap = 0.0
        for index, bounds in enumerate(row_bounds):
            overlap = min(bounds.y1, word.rect.y1) - max(bounds.y0, word.rect.y0)
            minimum_height = max(min(bounds.height, word.rect.height), 1e-6)
            ratio = overlap / minimum_height
            if ratio >= 0.3 and ratio > best_overlap:
                best_index = index
                best_overlap = ratio
        if best_index is None:
            rows.append([word])
            row_bounds.append(word.rect)
        else:
            rows[best_index].append(word)
            row_bounds[best_index] = _union((row_bounds[best_index], word.rect))

    return sorted(
        zip(row_bounds, rows),
        key=lambda item: (item[0].y0, item[0].x0),
    )


def _spanning_prefix_index(words: tuple[PdfWord, ...]) -> int | None:
    if len(words) < 3:
        return None

    candidate_index = min(
        range(len(words)),
        key=lambda index: (words[index].rect.x0, words[index].rect.y0),
    )
    candidate = words[candidate_index]
    remaining = tuple(
        word for index, word in enumerate(words) if index != candidate_index
    )
    if any(word.rect.x0 <= candidate.rect.x1 for word in remaining):
        return None

    right_rows = _ordered_rows(remaining)
    if len(right_rows) < 2:
        return None
    if candidate.rect.height < max(bounds.height for bounds, _row in right_rows) * 1.1:
        return None

    overlapping_rows = []
    for bounds, _row in right_rows:
        overlap = min(candidate.rect.y1, bounds.y1) - max(
            candidate.rect.y0,
            bounds.y0,
        )
        if overlap / max(bounds.height, 1e-6) >= 0.2:
            overlapping_rows.append(bounds)
    if len(overlapping_rows) < 2:
        return None

    candidate_center = (candidate.rect.y0 + candidate.rect.y1) / 2
    first_center = (overlapping_rows[0].y0 + overlapping_rows[0].y1) / 2
    last_center = (overlapping_rows[-1].y0 + overlapping_rows[-1].y1) / 2
    if not first_center < candidate_center < last_center:
        return None
    return candidate_index


def _row_text(row: list[PdfWord]) -> str:
    ordered = sorted(row, key=lambda item: (item.rect.x0, item.rect.y0))
    text = ordered[0].text
    previous = ordered[0]
    for word in ordered[1:]:
        gap = word.rect.x0 - previous.rect.x1
        join_tolerance = max(previous.rect.height, word.rect.height) * 0.1
        separator = "" if gap <= join_tolerance else " "
        text += separator + word.text
        previous = word
    return text


def _reconstruct_words_with_geometry(
    characters: tuple[PdfCharacter, ...],
    geometry_for,
) -> tuple[PdfWord, ...]:
    words: list[PdfWord] = []
    token: list[PdfCharacter] = []
    line_number = 0
    word_number = 0
    previous_geometry: PdfRect | None = None

    def flush() -> None:
        nonlocal word_number
        if not token:
            return
        words.append(
            PdfWord(
                rect=_union(character.rect for character in token),
                text="".join(character.text for character in token),
                block_number=0,
                line_number=line_number,
                word_number=word_number,
            )
        )
        word_number += 1
        token.clear()

    for character in characters:
        value = canonicalize_pdf_characters(character.text)
        if _is_line_break(value):
            flush()
            line_number += 1
            word_number = 0
            previous_geometry = None
            continue
        if not value or all(_is_separator(item) for item in value):
            flush()
            previous_geometry = None
            continue
        geometry = geometry_for(character)
        if previous_geometry is not None:
            if _starts_new_line(previous_geometry, geometry):
                flush()
                line_number += 1
                word_number = 0
            elif _starts_new_word(previous_geometry, geometry) or (
                _starts_trailing_number_word(
                    token,
                    value,
                    previous_geometry,
                    geometry,
                )
            ):
                flush()
        token.append(
            character
            if value == character.text
            else PdfCharacter(character.rect, value, character.origin)
        )
        previous_geometry = geometry
    flush()
    return tuple(words)


def _is_line_break(value: str) -> bool:
    return "\r" in value or "\n" in value


def _is_separator(value: str) -> bool:
    return value.isspace() or unicodedata.category(value).startswith("C")


def _starts_new_line(left: PdfRect, right: PdfRect) -> bool:
    overlap = min(left.y1, right.y1) - max(left.y0, right.y0)
    minimum_height = max(min(left.height, right.height), 1e-6)
    return overlap / minimum_height < 0.3


def _starts_new_word(left: PdfRect, right: PdfRect) -> bool:
    tolerance = max(left.height, right.height) * 0.5
    forward_gap = right.x0 - left.x1
    backward_gap = left.x0 - right.x1
    return forward_gap > tolerance or backward_gap > tolerance


def _starts_trailing_number_word(
    token: list[PdfCharacter],
    value: str,
    left: PdfRect,
    right: PdfRect,
) -> bool:
    if not value.isdigit() or len(token) < 2:
        return False
    token_text = "".join(character.text for character in token)
    if not token_text.isalpha():
        return False
    tolerance = max(left.height, right.height) * 0.4
    return right.x0 - left.x1 > tolerance


def _union(rects) -> PdfRect:
    values = tuple(rects)
    return PdfRect(
        min(rect.x0 for rect in values),
        min(rect.y0 for rect in values),
        max(rect.x1 for rect in values),
        max(rect.y1 for rect in values),
    )


def _unrotated_geometry(
    rect: PdfRect,
    space: PdfiumCoordinateSpace,
) -> PdfRect:
    native = space.app_to_pdfium(rect)
    return PdfRect(
        native.x0 - space.crop_box.x0,
        space.media_box.y1 - native.y1 - space.crop_box.y0,
        native.x1 - space.crop_box.x0,
        space.media_box.y1 - native.y0 - space.crop_box.y0,
    )
