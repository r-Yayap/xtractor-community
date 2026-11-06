from __future__ import annotations

import logging
import re
import unicodedata
from collections import Counter
from weakref import WeakKeyDictionary

from app.infra.pdf.contracts import PdfDocumentSession
from app.infra.pdf.errors import TextExtractionError
from app.infra.pdf.models import (
    DiagnosticCode,
    ExtractionDiagnostic,
    PdfCharacter,
    PdfRect,
    PdfWord,
    TextExtractionResult,
)
from app.infra.pdf.pdfium.common import require_session
from app.infra.pdf.pdfium.coordinates import PdfiumCoordinateSpace
from app.infra.pdf.pdfium.duplicate_overlay import (
    DuplicateCandidate,
    PageDuplicateEvidence,
    SuppressedAreaText,
    find_duplicate_candidates,
    inspect_page_objects,
    suppress_verified_area_text,
)
from app.infra.pdf.pdfium.layered_text import extract_layered_regions
from app.infra.pdf.pdfium.positioned_text import (
    _ordered_rows,
    characters_in_rect,
    reading_order_text,
    reconstruct_words,
)
from app.infra.pdf.text_normalization import (
    canonicalize_pdf_characters,
    collect_text_normalizations,
    normalize_pdf_text,
)

logger = logging.getLogger(__name__)

_SPLIT_UPPERCASE_GEOMETRY = re.compile(r"\b([A-Z]{2,})\s+([A-Z])\b")
_SMALL_LAYOUT_CHARACTER_LIMIT = 2048
_POSITIONED_CHARACTER_LIMIT = 20_000


class PdfiumTextCapability:
    def __init__(self) -> None:
        self._positioned_cache: WeakKeyDictionary = WeakKeyDictionary()
        self._duplicate_verification_cache: WeakKeyDictionary = WeakKeyDictionary()

    def extract_region(
        self,
        document: PdfDocumentSession,
        page_number: int,
        rect: PdfRect,
    ) -> TextExtractionResult:
        return self.extract_regions(document, page_number, (rect,))[0]

    def extract_regions(
        self,
        document: PdfDocumentSession,
        page_number: int,
        rects: tuple[PdfRect, ...],
    ) -> tuple[TextExtractionResult, ...]:
        session = require_session(document)
        try:
            with session.open_page(page_number) as page:
                space = _coordinate_space(page)
                resolved = tuple(rect.intersect(space.app_bounds) for rect in rects)
                text_page = page.get_textpage()
                try:
                    extracted: list[str | None] = []
                    for requested, effective in zip(rects, resolved):
                        if effective is None:
                            extracted.append(None)
                            continue
                        native = space.app_to_pdfium(effective)
                        extracted.append(
                            text_page.get_text_bounded(
                                left=native.x0,
                                bottom=native.y0,
                                right=native.x1,
                                top=native.y1,
                                errors="replace",
                            )
                        )
                    try:
                        character_count = text_page.count_chars()
                    except Exception:
                        character_count = 0
                    text_rectangles = (
                        _page_text_rectangles(text_page, space)
                        if _layout_rectangles_are_efficient(
                            text_page, character_count
                        )
                        else ()
                    )
                    positioned_characters = self._cached_page_characters(
                        session,
                        page_number,
                    )
                    if (
                        not text_rectangles
                        and 0 < character_count <= _POSITIONED_CHARACTER_LIMIT
                        and positioned_characters is None
                    ):
                        positioned_characters = self._page_characters(
                            session,
                            page_number,
                            text_page,
                            space,
                        )
                    area_inputs: list[
                        tuple[
                            PdfRect,
                            PdfRect | None,
                            str | None,
                            tuple[PdfWord, ...],
                            tuple[DuplicateCandidate, ...],
                        ]
                    ] = []
                    page_candidates: list[DuplicateCandidate] = []
                    for requested, effective, value in zip(
                        rects,
                        resolved,
                        extracted,
                    ):
                        if effective is None or value is None:
                            area_inputs.append((requested, None, None, (), ()))
                            continue
                        if text_rectangles:
                            words = _words_in_region_from_text_rectangles(
                                text_page,
                                space,
                                effective,
                                text_rectangles,
                            )
                        elif positioned_characters is not None:
                            characters = tuple(
                                character
                                for character in positioned_characters
                                if character.rect.intersect(effective)
                                is not None
                            )
                            words = reconstruct_words(characters, space)
                        else:
                            words = ()
                        if _layout_requires_character_repair(value, words):
                            if positioned_characters is None:
                                positioned_characters = self._page_characters(
                                    session,
                                    page_number,
                                    text_page,
                                    space,
                                )
                            characters = tuple(
                                character
                                for character in positioned_characters
                                if character.rect.intersect(effective)
                                is not None
                            )
                            words = reconstruct_words(characters, space)
                        candidates = find_duplicate_candidates(words)
                        page_candidates.extend(candidates)
                        area_inputs.append(
                            (requested, effective, value, words, candidates)
                        )

                    duplicate_evidence = self._page_duplicate_evidence(
                        session,
                        page_number,
                        page,
                        text_page,
                        space,
                        tuple(dict.fromkeys(page_candidates)),
                        character_count,
                    )
                    results: list[TextExtractionResult] = []
                    for requested, effective, value, words, candidates in area_inputs:
                        if effective is None or value is None:
                            results.append(
                                _invalid_clip_result(page_number, requested)
                            )
                            continue
                        raw_value = value
                        suppressed = ()
                        area_suppression = _suppress_area_text_safely(
                            value,
                            words,
                            effective,
                            candidates,
                            duplicate_evidence,
                        )
                        if area_suppression is not None:
                            raw_value = area_suppression.text
                            words = area_suppression.words
                            suppressed = area_suppression.suppressed
                        repaired = _repair_missing_word_separators(
                            raw_value,
                            words,
                        )
                        results.append(
                            TextExtractionResult(
                                repaired,
                                suppressed=suppressed,
                                normalizations=collect_text_normalizations(raw_value),
                            )
                        )
                    if character_count > _POSITIONED_CHARACTER_LIMIT:
                        valid_indexes = tuple(
                            index
                            for index, effective in enumerate(resolved)
                            if effective is not None
                        )
                        if valid_indexes:
                            layered = extract_layered_regions(
                                page,
                                space,
                                tuple(resolved[index] for index in valid_indexes),
                            )
                            for index, cell in zip(valid_indexes, layered):
                                if cell.display != cell.primary:
                                    original = results[index]
                                    results[index] = TextExtractionResult(
                                        cell.display,
                                        diagnostics=original.diagnostics,
                                        suppressed=original.suppressed,
                                        normalizations=original.normalizations,
                                    )
                    return tuple(results)
                finally:
                    text_page.close()
        except Exception as exc:
            logger.warning(
                "PDFium text extraction failed on page %s (%s)",
                page_number,
                type(exc).__name__,
            )
            raise TextExtractionError(
                f"Could not extract text from page {page_number}"
            ) from exc

    def extract_native_regions(
        self,
        document: PdfDocumentSession,
        page_number: int,
        rects: tuple[PdfRect, ...],
    ) -> tuple[TextExtractionResult, ...]:
        """Extract bounded text without the full-page character-box scan."""
        session = require_session(document)
        try:
            with session.open_page(page_number) as page:
                space = _coordinate_space(page)
                resolved = tuple(rect.intersect(space.app_bounds) for rect in rects)
                text_page = page.get_textpage()
                try:
                    values: list[str | None] = []
                    for effective in resolved:
                        if effective is None:
                            values.append(None)
                            continue
                        native = space.app_to_pdfium(effective)
                        values.append(
                            text_page.get_text_bounded(
                                left=native.x0,
                                bottom=native.y0,
                                right=native.x1,
                                top=native.y1,
                                errors="replace",
                            )
                        )

                    try:
                        character_count = text_page.count_chars()
                    except Exception:
                        character_count = 0
                    text_rectangles: tuple[PdfRect, ...] = ()
                    if (
                        character_count <= _POSITIONED_CHARACTER_LIMIT
                        and any(
                            _has_repeated_visible_subsequence_hint(value)
                            for value in values
                            if value
                        )
                        and _layout_rectangles_are_efficient(
                            text_page,
                            character_count,
                        )
                    ):
                        text_rectangles = _page_text_rectangles(text_page, space)

                    area_words: list[tuple[PdfWord, ...]] = []
                    area_candidates: list[tuple[DuplicateCandidate, ...]] = []
                    page_candidates: list[DuplicateCandidate] = []
                    for effective, value in zip(resolved, values):
                        words = (
                            _words_in_region_from_text_rectangles(
                                text_page,
                                space,
                                effective,
                                text_rectangles,
                            )
                            if effective is not None and text_rectangles
                            else ()
                        )
                        candidates = find_duplicate_candidates(words)
                        area_words.append(words)
                        area_candidates.append(candidates)
                        page_candidates.extend(candidates)

                    duplicate_evidence = self._page_duplicate_evidence(
                        session,
                        page_number,
                        page,
                        text_page,
                        space,
                        tuple(dict.fromkeys(page_candidates)),
                        character_count,
                    )
                    results: list[TextExtractionResult] = []
                    for requested, effective, value, words, candidates in zip(
                        rects,
                        resolved,
                        values,
                        area_words,
                        area_candidates,
                    ):
                        if effective is None or value is None:
                            results.append(
                                _invalid_clip_result(page_number, requested)
                            )
                            continue
                        raw_value = value
                        suppressed = ()
                        area_suppression = _suppress_area_text_safely(
                            value,
                            words,
                            effective,
                            candidates,
                            duplicate_evidence,
                        )
                        if area_suppression is not None:
                            raw_value = area_suppression.text
                            suppressed = area_suppression.suppressed
                        results.append(
                            TextExtractionResult(
                                raw_value,
                                suppressed=suppressed,
                                normalizations=collect_text_normalizations(raw_value),
                            )
                        )
                    return tuple(results)
                finally:
                    text_page.close()
        except Exception as exc:
            logger.warning(
                "PDFium native bounded text extraction failed on page %s (%s)",
                page_number,
                type(exc).__name__,
            )
            raise TextExtractionError(
                f"Could not extract bounded text from page {page_number}"
            ) from exc

    def extract_characters(
        self,
        document: PdfDocumentSession,
        page_number: int,
        rect: PdfRect,
    ) -> tuple[PdfCharacter, ...]:
        session = require_session(document)
        try:
            with session.open_page(page_number) as page:
                space = _coordinate_space(page)
                effective = rect.intersect(space.app_bounds)
                if effective is None:
                    return ()
                cached = self._cached_page_characters(session, page_number)
                if cached is None:
                    text_page = page.get_textpage()
                    try:
                        cached = self._page_characters(
                            session,
                            page_number,
                            text_page,
                            space,
                        )
                    finally:
                        text_page.close()
                return tuple(
                    character
                    for character in cached
                    if character.rect.intersect(effective) is not None
                )
        except Exception as exc:
            raise TextExtractionError(
                f"Could not extract characters from page {page_number}"
            ) from exc

    def extract_words(
        self,
        document: PdfDocumentSession,
        page_number: int,
        rect: PdfRect,
    ) -> tuple[PdfWord, ...]:
        session = require_session(document)
        with session.open_page(page_number) as page:
            space = _coordinate_space(page)
            effective = rect.intersect(space.app_bounds)
            if effective is None:
                return ()
            cached = self._cached_page_characters(session, page_number)
            if cached is None:
                text_page = page.get_textpage()
                try:
                    try:
                        character_count = text_page.count_chars()
                    except Exception:
                        character_count = 0
                    if character_count > _POSITIONED_CHARACTER_LIMIT:
                        rectangles = _page_text_rectangles(text_page, space)
                        return _words_in_region_from_text_rectangles(
                            text_page,
                            space,
                            effective,
                            rectangles,
                        )
                    cached = self._page_characters(
                        session,
                        page_number,
                        text_page,
                        space,
                    )
                finally:
                    text_page.close()
            characters = tuple(
                character
                for character in cached
                if character.rect.intersect(effective) is not None
            )
        return reconstruct_words(characters, space)

    def can_repair_regions_from_positioned_text(
        self,
        document: PdfDocumentSession,
        page_number: int,
    ) -> bool:
        session = require_session(document)
        if self._cached_page_characters(session, page_number) is not None:
            return True
        try:
            with session.open_page(page_number) as page:
                text_page = page.get_textpage()
                try:
                    character_count = text_page.count_chars()
                finally:
                    text_page.close()
        except Exception:
            return False
        return 0 < character_count <= _POSITIONED_CHARACTER_LIMIT

    def _cached_page_characters(
        self,
        session,
        page_number: int,
    ) -> tuple[PdfCharacter, ...] | None:
        pages = self._positioned_cache.get(session)
        return None if pages is None else pages.get(page_number)

    def _page_characters(
        self,
        session,
        page_number: int,
        text_page,
        space: PdfiumCoordinateSpace,
    ) -> tuple[PdfCharacter, ...]:
        cached = self._cached_page_characters(session, page_number)
        if cached is not None:
            return cached
        characters = characters_in_rect(text_page, space, space.app_bounds)
        pages = self._positioned_cache.setdefault(session, {})
        pages[page_number] = characters
        return characters

    def _page_duplicate_evidence(
        self,
        session,
        page_number: int,
        page,
        text_page,
        space: PdfiumCoordinateSpace,
        candidates: tuple[DuplicateCandidate, ...],
        character_count: int,
    ) -> PageDuplicateEvidence | None:
        if not candidates or character_count > _POSITIONED_CHARACTER_LIMIT:
            return None
        pages = self._duplicate_verification_cache.setdefault(session, {})
        if page_number in pages:
            return pages[page_number]
        try:
            evidence = inspect_page_objects(
                page,
                text_page,
                space,
                candidates,
                character_limit=_POSITIONED_CHARACTER_LIMIT,
            )
        except Exception as exc:
            logger.debug(
                "PDFium duplicate candidate verification failed (%s)",
                type(exc).__name__,
            )
            evidence = None
        pages[page_number] = evidence
        return evidence


def _coordinate_space(page) -> PdfiumCoordinateSpace:
    return PdfiumCoordinateSpace.from_page(page)


def _suppress_area_text_safely(
    value: str,
    words: tuple[PdfWord, ...],
    region: PdfRect,
    candidates: tuple[DuplicateCandidate, ...],
    evidence: PageDuplicateEvidence | None,
) -> SuppressedAreaText | None:
    try:
        return suppress_verified_area_text(
            value,
            words,
            region,
            candidates,
            evidence,
        )
    except Exception as exc:
        logger.debug(
            "PDFium duplicate suppression skipped (%s)",
            type(exc).__name__,
        )
        return None


def _invalid_clip_result(page_number: int, rect: PdfRect) -> TextExtractionResult:
    return TextExtractionResult(
        "",
        (
            ExtractionDiagnostic(
                DiagnosticCode.INVALID_CLIP,
                "Region did not intersect the page.",
                page_number=page_number,
                requested_rect=rect,
            ),
        ),
    )


def _repair_missing_word_separators(
    value: str,
    words: tuple[PdfWord, ...],
) -> str:
    """Use character geometry only when it represents the same extracted text."""
    if not value or not words:
        return value
    geometry_text = reading_order_text(words)
    geometry_characters = _visible_character_counts(geometry_text)
    bounded_characters = _visible_character_counts(value)
    if not geometry_characters or geometry_characters != bounded_characters:
        return value
    bounded_sequence, _ = _visible_sequence_and_boundaries(value)
    geometry_sequence, geometry_boundaries = _visible_sequence_and_boundaries(
        geometry_text
    )
    if bounded_sequence == geometry_sequence:
        return _repair_geometry_lines(
            value,
            geometry_text,
            geometry_boundaries,
        )
    for match in _SPLIT_UPPERCASE_GEOMETRY.finditer(geometry_text):
        if "".join(match.groups()) in value:
            return value
    return geometry_text


def _repair_geometry_lines(
    value: str,
    geometry_text: str,
    geometry_boundaries: frozenset[int],
) -> str:
    canonical_value = canonicalize_pdf_characters(value)
    if len(canonical_value) != len(value):
        return value

    offsets = _visible_character_offsets(value)
    if not offsets:
        return value

    lines = geometry_text.splitlines()
    bounded_sequence, _ = _visible_sequence_and_boundaries(value)
    line_sequences = tuple(
        _visible_sequence_and_boundaries(line)[0] for line in lines
    )
    if "".join(line_sequences) != bounded_sequence:
        return value
    if not line_sequences or any(not line_sequence for line_sequence in line_sequences):
        return value

    row_boundaries = _geometry_row_boundaries(line_sequences, offsets)
    if row_boundaries is None:
        return value

    explicit_boundaries = _explicit_separator_boundaries(value)
    chunks: list[str] = []
    cursor = 0
    changed = False
    for line, line_sequence in zip(lines, line_sequences):
        start = cursor
        end = start + len(line_sequence)
        raw_segment = value[offsets[start] : offsets[end - 1] + 1]
        safe = all(
            boundary in geometry_boundaries
            for boundary in explicit_boundaries
            if start <= boundary <= end
        )
        if safe:
            raw_sequence = _visible_sequence_and_boundaries(raw_segment)[0]
            safe = not any(
                "".join(match.groups()) in raw_sequence
                for match in _SPLIT_UPPERCASE_GEOMETRY.finditer(line)
            )
        replacement = line if safe else raw_segment
        chunks.append(replacement)
        changed |= safe and replacement != raw_segment
        cursor = end

    if changed:
        return _join_repaired_geometry_lines(
            value,
            canonical_value,
            chunks,
            row_boundaries,
            explicit_boundaries,
        )
    return _insert_missing_geometry_row_boundaries(
        value,
        canonical_value,
        row_boundaries,
    )


def _geometry_row_boundaries(
    line_sequences: tuple[str, ...],
    offsets: tuple[int, ...],
) -> tuple[tuple[int, int, int], ...] | None:
    visible_boundary = 0
    boundaries: list[tuple[int, int, int]] = []
    for line_sequence in line_sequences[:-1]:
        visible_boundary += len(line_sequence)
        if not 0 < visible_boundary < len(offsets):
            return None
        boundaries.append(
            (
                offsets[visible_boundary - 1],
                offsets[visible_boundary],
                visible_boundary,
            )
        )
    return tuple(boundaries)


def _join_repaired_geometry_lines(
    value: str,
    canonical_value: str,
    chunks: list[str],
    row_boundaries: tuple[tuple[int, int, int], ...],
    explicit_boundaries: frozenset[int],
) -> str:
    output: list[str] = []
    for index, chunk in enumerate(chunks):
        output.append(chunk)
        if index == len(chunks) - 1:
            continue
        left_index, right_index, visible_boundary = row_boundaries[index]
        between = value[left_index + 1 : right_index]
        if visible_boundary in explicit_boundaries:
            output.append(between)
            continue
        if not between and canonical_value[left_index] == "-":
            continue
        output.append("\n")
    return "".join(output)


def _insert_missing_geometry_row_boundaries(
    value: str,
    canonical_value: str,
    row_boundaries: tuple[tuple[int, int, int], ...],
) -> str:
    insertions: list[int] = []
    for left_index, right_index, _ in row_boundaries:
        between = value[left_index + 1 : right_index]
        if between or canonical_value[left_index] == "-":
            continue
        insertions.append(right_index)

    if not insertions:
        return value

    output: list[str] = []
    cursor = 0
    for index in insertions:
        output.extend((value[cursor:index], "\n"))
        cursor = index
    output.append(value[cursor:])
    return "".join(output)


def _explicit_separator_boundaries(value: str) -> frozenset[int]:
    """Return native separator positions other than CR/LF fragmentation."""
    characters: list[str] = []
    boundaries: set[int] = set()
    pending_boundary = False
    for character in canonicalize_pdf_characters(value):
        if character in "\r\n":
            continue
        if character.isspace() or unicodedata.category(character).startswith("C"):
            pending_boundary = bool(characters)
            continue
        if pending_boundary:
            boundaries.add(len(characters))
            pending_boundary = False
        characters.append(character)
    return frozenset(boundaries)


def _visible_character_offsets(value: str) -> tuple[int, ...]:
    return tuple(
        index
        for index, character in enumerate(canonicalize_pdf_characters(value))
        if not character.isspace()
        and not unicodedata.category(character).startswith("C")
    )


def _visible_sequence_and_boundaries(
    value: str,
) -> tuple[str, frozenset[int]]:
    characters: list[str] = []
    boundaries: set[int] = set()
    pending_boundary = False
    for character in canonicalize_pdf_characters(value):
        if character.isspace() or unicodedata.category(character).startswith("C"):
            pending_boundary = bool(characters)
            continue
        if pending_boundary:
            boundaries.add(len(characters))
            pending_boundary = False
        characters.append(character)
    return "".join(characters), frozenset(boundaries)


def _has_repeated_visible_subsequence_hint(value: str) -> bool:
    """Cheap native-only prefilter; never sufficient for a duplicate candidate."""
    visible, _boundaries = _visible_sequence_and_boundaries(value)
    if len(visible) < 2:
        return False
    if len(visible) < 6:
        return any(
            count > 1
            for count in (
                *Counter(visible).values(),
                *Counter(
                    visible[index : index + 2]
                    for index in range(len(visible) - 1)
                ).values(),
            )
        )
    trigrams = Counter(
        visible[index : index + 3]
        for index in range(len(visible) - 2)
    )
    return any(count > 1 for count in trigrams.values())


def _page_text_rectangles(
    text_page,
    space: PdfiumCoordinateSpace,
) -> tuple[PdfRect, ...]:
    """Return PDFium's inexpensive positioned text fragments in app space."""
    try:
        count = text_page.count_rects()
        return tuple(
            space.pdfium_to_app(PdfRect.from_value(text_page.get_rect(index)))
            for index in range(count)
        )
    except Exception:
        # Bounded text remains a safe extraction fallback if a PDFium build
        # cannot expose layout rectangles for a malformed page.
        return ()


def _layout_rectangles_are_efficient(text_page, character_count: int) -> bool:
    if character_count <= _SMALL_LAYOUT_CHARACTER_LIMIT:
        return True
    try:
        rectangle_count = text_page.count_rects()
    except Exception:
        return False
    # Some CAD PDFs expose one rectangle per glyph. Bounded extraction is
    # faster for those pages; ordinary text runs expose far fewer rectangles
    # than characters and remain cheap even on very large drawings.
    return rectangle_count * 2 <= character_count


def _words_in_region_from_text_rectangles(
    text_page,
    space: PdfiumCoordinateSpace,
    region: PdfRect,
    text_rectangles: tuple[PdfRect, ...],
) -> tuple[PdfWord, ...]:
    """Extract visually positioned fragments without scanning every char box."""
    words: list[PdfWord] = []
    for source_rect in text_rectangles:
        clipped = source_rect.intersect(region)
        if clipped is None:
            continue
        native = space.app_to_pdfium(clipped)
        value = text_page.get_text_bounded(
            left=native.x0,
            bottom=native.y0,
            right=native.x1,
            top=native.y1,
            errors="replace",
        )
        value = normalize_pdf_text(value)
        if not value:
            continue
        words.append(
            PdfWord(
                rect=clipped,
                text=value,
                block_number=0,
                line_number=len(words),
                word_number=0,
            )
        )
    return tuple(words)


def _visible_character_counts(value: str) -> Counter[str]:
    value = canonicalize_pdf_characters(value)
    return Counter(
        character
        for character in value
        if not character.isspace()
        and not unicodedata.category(character).startswith("C")
    )


def _layout_requires_character_repair(
    value: str,
    words: tuple[PdfWord, ...],
) -> bool:
    if not value or len(words) < 2:
        return False
    geometry_text = reading_order_text(words)
    if _visible_character_counts(geometry_text) == _visible_character_counts(value):
        if _layout_has_singleton_fragment_issue(value, words):
            return True
        return False
    for index, left in enumerate(words):
        left_area = left.rect.width * left.rect.height
        if left_area <= 0:
            continue
        for right in words[index + 1 :]:
            right_area = right.rect.width * right.rect.height
            if right_area <= 0:
                continue
            overlap = left.rect.intersect(right.rect)
            if overlap is None:
                continue
            overlap_area = overlap.width * overlap.height
            if overlap_area / min(left_area, right_area) >= 0.65:
                return True
    return False


def _layout_has_singleton_fragment_issue(
    value: str,
    words: tuple[PdfWord, ...],
) -> bool:
    if not value or len(words) < 2:
        return False
    geometry_text = reading_order_text(words)
    native_sequence, native_boundaries = _visible_sequence_and_boundaries(value)
    geometry_sequence, geometry_boundaries = _visible_sequence_and_boundaries(
        geometry_text
    )
    if not native_sequence or native_sequence != geometry_sequence:
        return False
    if any(
        "".join(match.groups()) in native_sequence
        for match in _SPLIT_UPPERCASE_GEOMETRY.finditer(geometry_text)
    ):
        return False
    if native_sequence.isupper():
        return False
    extra_boundaries = geometry_boundaries - native_boundaries
    if not extra_boundaries:
        return False

    visible_cursor = 0
    for _bounds, row in _ordered_rows(words):
        ordered = sorted(row, key=lambda item: (item.rect.x0, item.rect.y0))
        for word in ordered:
            sequence, _word_boundaries = _visible_sequence_and_boundaries(word.text)
            if (
                len(sequence) == 1
                and sequence.isalnum()
                and (
                    visible_cursor in extra_boundaries
                    or visible_cursor + 1 in extra_boundaries
                )
            ):
                return True
            visible_cursor += len(sequence)
    return False
