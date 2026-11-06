from __future__ import annotations

import ctypes
import math
import unicodedata
from collections import Counter
from dataclasses import dataclass, replace

from app.infra.pdf.models import (
    PdfRect,
    PdfWord,
    SuppressedTextOccurrence,
)
from app.infra.pdf.pdfium.coordinates import PdfiumCoordinateSpace
from app.infra.pdf.text_normalization import canonicalize_pdf_characters

# PDFium exposes most page/glyph geometry as single-precision values. Four
# float32 ULPs at ordinary drawing coordinates are below this tolerance.
_GEOMETRY_TOLERANCE = 0.001


@dataclass(frozen=True, slots=True)
class DuplicateCandidate:
    text: str
    rect: PdfRect


@dataclass(frozen=True, slots=True)
class GlyphEvidence:
    text: str
    char_index: int
    rect: PdfRect
    origin: tuple[float, float]
    angle: float


@dataclass(frozen=True, slots=True)
class NativeObjectEvidence:
    """Copied PDFium page-object state; contains no native handles."""

    sequence: int
    kind: str
    rect: PdfRect | None
    text: str | None = None
    font_identity: int | None = None
    font_name: str | None = None
    font_size: float | None = None
    matrix: tuple[float, ...] | None = None
    glyphs: tuple[GlyphEvidence, ...] = ()
    fill_rgba: tuple[int, int, int, int] | None = None
    stroke_rgba: tuple[int, int, int, int] | None = None
    render_mode: int | None = None
    has_transparency: bool = True
    has_clip: bool = True
    has_marks: bool = True
    form_depth: int = 1


@dataclass(frozen=True, slots=True)
class VerifiedDuplicateGroup:
    text: str
    rect: PdfRect
    candidate_text: str
    candidate_rect: PdfRect
    font: str
    font_size: float
    kept_sequence: int
    suppressed_sequence: int
    kept_char_indices: tuple[int, ...]
    suppressed_char_indices: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class PageGlyphEvidence:
    text: str
    char_index: int
    rect: PdfRect | None
    owner_sequence: int | None


@dataclass(frozen=True, slots=True)
class PageDuplicateEvidence:
    objects: tuple[NativeObjectEvidence, ...]
    glyphs: tuple[PageGlyphEvidence, ...]
    mapping_safe: bool


@dataclass(frozen=True, slots=True)
class SuppressedAreaText:
    text: str
    words: tuple[PdfWord, ...]
    suppressed: tuple[SuppressedTextOccurrence, ...]


def find_duplicate_candidates(
    words: tuple[PdfWord, ...],
) -> tuple[DuplicateCandidate, ...]:
    """Find exact repeated word/geometry pairs; text repetition alone is inert."""
    counts: Counter[tuple[str, tuple[float, float, float, float]]] = Counter()
    rects: dict[tuple[str, tuple[float, float, float, float]], PdfRect] = {}
    for word in words:
        text = word.text
        rect = word.rect.normalized()
        if not text or not rect.is_valid():
            continue
        key = (text, rect.to_tuple())
        counts[key] += 1
        rects[key] = rect

    candidates = (
        DuplicateCandidate(text, rects[(text, geometry)])
        for (text, geometry), count in counts.items()
        if count > 1
    )
    return tuple(
        sorted(
            candidates,
            key=lambda candidate: (
                candidate.rect.y0,
                candidate.rect.x0,
                candidate.text,
            ),
        )
    )


def verify_candidate_groups(
    candidates: tuple[DuplicateCandidate, ...],
    objects: tuple[NativeObjectEvidence, ...],
) -> tuple[VerifiedDuplicateGroup, ...]:
    """Return only fully supported duplicate pairs, otherwise fail closed."""
    if not candidates or not objects:
        return ()

    ordered_objects = tuple(sorted(objects, key=lambda item: item.sequence))
    if len({item.sequence for item in ordered_objects}) != len(ordered_objects):
        return ()

    duplicate_candidates = tuple(
        sorted(
            set(candidates),
            key=lambda item: (item.rect.y0, item.rect.x0, item.text),
        )
    )
    verified: list[VerifiedDuplicateGroup] = []
    for candidate in duplicate_candidates:
        spatial_matches = tuple(
            item
            for item in ordered_objects
            if item.kind == "text"
            and _rectangles_equivalent(item.rect, candidate.rect)
        )
        text_counts = Counter(
            item.text for item in spatial_matches if item.text
        )
        matching_texts = tuple(
            text
            for text, count in text_counts.items()
            if count >= 2 and _is_exact_repetition(candidate.text, text)
        )
        if len(matching_texts) > 1:
            continue
        object_text = (
            matching_texts[0]
            if matching_texts
            else candidate.text
        )
        matching = tuple(
            item for item in spatial_matches if item.text == object_text
        )
        if len(matching) < 2 or len(matching) != len(spatial_matches):
            continue

        supported = tuple(
            item
            for item in matching
            if _is_supported_text_object(item, candidate)
        )
        if len(supported) != len(matching):
            continue

        signatures: list[list[NativeObjectEvidence]] = []
        for item in supported:
            group = next(
                (
                    existing
                    for existing in signatures
                    if _same_visual_text_state(existing[0], item)
                ),
                None,
            )
            if group is None:
                signatures.append([item])
            else:
                group.append(item)

        for same_state in signatures:
            if len(same_state) < 2:
                continue
            if not _has_unambiguous_char_mapping(same_state):
                continue
            members = {item.sequence for item in same_state}
            first = same_state[0]
            last = same_state[-1]
            if _intervening_paint_may_overlap(
                ordered_objects,
                members,
                first.sequence,
                last.sequence,
                candidate.rect,
            ):
                continue
            for later in same_state[1:]:
                verified.append(
                    VerifiedDuplicateGroup(
                        text=object_text,
                        rect=first.rect or candidate.rect,
                        candidate_text=candidate.text,
                        candidate_rect=candidate.rect,
                        font=first.font_name or "",
                        font_size=first.font_size or 0.0,
                        kept_sequence=first.sequence,
                        suppressed_sequence=later.sequence,
                        kept_char_indices=tuple(
                            glyph.char_index for glyph in first.glyphs
                        ),
                        suppressed_char_indices=tuple(
                            glyph.char_index for glyph in later.glyphs
                        ),
                    )
                )

    return tuple(
        sorted(
            verified,
            key=lambda item: (
                item.rect.y0,
                item.rect.x0,
                item.text,
                item.suppressed_sequence,
            ),
        )
    )


def inspect_page_objects(
    page,
    text_page,
    space: PdfiumCoordinateSpace,
    candidates: tuple[DuplicateCandidate, ...],
    *,
    character_limit: int = 20_000,
) -> PageDuplicateEvidence | None:
    """Copy candidate-page PDFium metadata into immutable Python records.

    All PDFium object and text-page handles remain local to this call. The
    caller may cache the returned snapshot for the life of its document
    session; the snapshot contains only scalar values and tuples.
    """
    if not candidates:
        return None

    from pypdfium2 import raw as pdfium_raw

    character_count = int(text_page.count_chars())
    if character_count < 0 or character_count > character_limit:
        return None

    page_transparent = bool(pdfium_raw.FPDFPage_HasTransparency(page.raw))
    page_objects = tuple(
        page.get_objects(max_depth=1, textpage=text_page)
    )

    object_tokens: dict[int, int] = {}
    native_objects: list[NativeObjectEvidence] = []
    text_object_evidence: dict[int, NativeObjectEvidence] = {}
    font_tokens: dict[int, int] = {}

    for sequence, page_object in enumerate(page_objects):
        token = _native_handle_token(page_object.raw)
        if token is None or token in object_tokens:
            raise ValueError("PDFium page object identity is ambiguous")
        object_tokens[token] = sequence
        kind = _object_kind(page_object.type, pdfium_raw)
        rect = _page_object_rect(page_object, space)
        preliminary = NativeObjectEvidence(
            sequence=sequence,
            kind=kind,
            rect=rect,
        )
        native_objects.append(preliminary)

        if kind != "text":
            continue

        try:
            object_text = page_object.extract()
        except Exception:
            evidence = replace(
                preliminary,
                text=None,
                has_transparency=True,
            )
            native_objects[sequence] = evidence
            text_object_evidence[sequence] = evidence
            continue

        try:
            font = page_object.get_font()
            font_token = _native_handle_token(font.raw)
            if font_token is None:
                raise ValueError("PDFium font identity is unavailable")
            if font_token not in font_tokens:
                font_tokens[font_token] = len(font_tokens)
            font_identity = font_tokens[font_token]
            font_name = font.get_base_name()
            font_size = float(page_object.get_font_size())
            matrix_value = page_object.get_matrix()
            matrix = tuple(
                float(value)
                for value in (
                    matrix_value.a,
                    matrix_value.b,
                    matrix_value.c,
                    matrix_value.d,
                    matrix_value.e,
                    matrix_value.f,
                )
            )
            fill_rgba = _object_color(
                pdfium_raw.FPDFPageObj_GetFillColor,
                page_object.raw,
            )
            stroke_rgba = _object_color(
                pdfium_raw.FPDFPageObj_GetStrokeColor,
                page_object.raw,
            )
            render_mode = int(
                pdfium_raw.FPDFTextObj_GetTextRenderMode(page_object.raw)
            )
            has_transparency = bool(
                pdfium_raw.FPDFPageObj_HasTransparency(page_object.raw)
            ) or page_transparent
            has_clip = _object_has_clip(page_object.raw, pdfium_raw)
            mark_count = int(pdfium_raw.FPDFPageObj_CountMarks(page_object.raw))
            marked_content_id = int(
                pdfium_raw.FPDFPageObj_GetMarkedContentID(page_object.raw)
            )
            has_marks = (
                mark_count != 0
                or marked_content_id != -1
                or mark_count < 0
            )
            form_depth = int(page_object.level)
            if page_object.container is not None:
                form_depth = max(1, form_depth)
            evidence = NativeObjectEvidence(
                sequence=sequence,
                kind=kind,
                rect=rect,
                text=object_text,
                font_identity=font_identity,
                font_name=font_name,
                font_size=font_size,
                matrix=matrix,
                fill_rgba=fill_rgba,
                stroke_rgba=stroke_rgba,
                render_mode=render_mode,
                has_transparency=has_transparency,
                has_clip=has_clip,
                has_marks=has_marks,
                form_depth=form_depth,
            )
        except Exception:
            evidence = replace(
                preliminary,
                text=object_text,
                has_transparency=True,
            )
        native_objects[sequence] = evidence
        text_object_evidence[sequence] = evidence

    candidate_sequences = set(text_object_evidence)
    object_sequence_by_token = object_tokens
    glyphs_by_object: dict[int, list[GlyphEvidence]] = {
        sequence: [] for sequence in candidate_sequences
    }
    glyphs: list[PageGlyphEvidence] = []
    mapping_safe = True

    for char_index in range(character_count):
        codepoint = int(pdfium_raw.FPDFText_GetUnicode(text_page, char_index))
        if (
            codepoint < 0
            or codepoint > 0x10FFFF
            or 0xD800 <= codepoint <= 0xDFFF
        ):
            raise ValueError("PDFium returned an invalid Unicode codepoint")
        character = chr(codepoint)
        owner = pdfium_raw.FPDFText_GetTextObject(text_page, char_index)
        owner_token = _native_handle_token(owner)
        owner_sequence = object_sequence_by_token.get(owner_token)
        try:
            native_rect = PdfRect.from_value(text_page.get_charbox(char_index))
            rect = space.pdfium_to_app(native_rect)
        except Exception:
            rect = None
            if _is_visible_character(character):
                mapping_safe = False
            if owner_sequence in candidate_sequences:
                glyphs_by_object[owner_sequence].clear()
        glyphs.append(
            PageGlyphEvidence(
                text=character,
                char_index=char_index,
                rect=rect,
                owner_sequence=owner_sequence,
            )
        )

        if owner_sequence not in candidate_sequences or rect is None:
            continue
        try:
            origin_x = ctypes.c_double()
            origin_y = ctypes.c_double()
            if not pdfium_raw.FPDFText_GetCharOrigin(
                text_page,
                char_index,
                ctypes.byref(origin_x),
                ctypes.byref(origin_y),
            ):
                raise ValueError("PDFium glyph origin is unavailable")
            origin_point = space.pdfium_to_app(
                PdfRect(
                    origin_x.value,
                    origin_y.value,
                    origin_x.value,
                    origin_y.value,
                )
            )
            angle = float(
                pdfium_raw.FPDFText_GetCharAngle(text_page, char_index)
            )
            glyphs_by_object[owner_sequence].append(
                GlyphEvidence(
                    text=character,
                    char_index=char_index,
                    rect=rect,
                    origin=(origin_point.x0, origin_point.y0),
                    angle=angle,
                )
            )
        except Exception:
            glyphs_by_object[owner_sequence].clear()

    for sequence, evidence in text_object_evidence.items():
        native_objects[sequence] = replace(
            evidence,
            glyphs=tuple(glyphs_by_object[sequence]),
            has_transparency=evidence.has_transparency or page_transparent,
        )

    return PageDuplicateEvidence(
        objects=tuple(native_objects),
        glyphs=tuple(glyphs),
        mapping_safe=mapping_safe,
    )


def suppress_verified_area_text(
    value: str,
    words: tuple[PdfWord, ...],
    region: PdfRect,
    candidates: tuple[DuplicateCandidate, ...],
    evidence: PageDuplicateEvidence | None,
) -> SuppressedAreaText | None:
    """Remove only character indices owned by verified later page objects."""
    if not candidates or evidence is None or not evidence.mapping_safe:
        return None

    verified = verify_candidate_groups(candidates, evidence.objects)
    if not verified:
        return None

    objects_by_sequence = {item.sequence: item for item in evidence.objects}
    candidate_keys = {_candidate_key(item) for item in candidates}
    group_counts: Counter[tuple[str, tuple[float, float, float, float]]] = Counter()
    groups_by_key: dict[
        tuple[str, tuple[float, float, float, float]], list[VerifiedDuplicateGroup]
    ] = {}
    for item in verified:
        key = (item.candidate_text, item.candidate_rect.normalized().to_tuple())
        if key in candidate_keys:
            groups_by_key.setdefault(key, []).append(item)
            group_counts[key] += 1

    if not groups_by_key:
        return None

    word_counts = Counter(_word_key(word) for word in words)
    accepted_groups: list[VerifiedDuplicateGroup] = []
    remove_word_counts: Counter[
        tuple[str, tuple[float, float, float, float]]
    ] = Counter()
    for key, groups in groups_by_key.items():
        if word_counts[key] <= group_counts[key]:
            continue
        group_is_contained = True
        for group in groups:
            for sequence in (group.kept_sequence, group.suppressed_sequence):
                native_object = objects_by_sequence.get(sequence)
                if native_object is None or not _object_fully_contained(
                    native_object,
                    region,
                ):
                    group_is_contained = False
                    break
            if not group_is_contained:
                break
        if not group_is_contained:
            continue
        accepted_groups.extend(groups)
        remove_word_counts[key] += len(groups)

    if not accepted_groups:
        return None

    value_offsets, visible_page_glyphs = _visible_value_mapping(
        value,
        evidence.glyphs,
        region,
    )
    if value_offsets is None:
        return None

    glyph_by_index = {
        glyph.char_index: glyph for glyph in visible_page_glyphs
    }
    removed_offsets: set[int] = set()
    suppressed: list[SuppressedTextOccurrence] = []
    for group in accepted_groups:
        kept = tuple(
            glyph_by_index.get(index) for index in group.kept_char_indices
        )
        removed = tuple(
            glyph_by_index.get(index)
            for index in group.suppressed_char_indices
        )
        if any(item is None for item in (*kept, *removed)):
            return None
        if any(
            item.owner_sequence != group.kept_sequence
            for item in kept
            if item is not None
        ) or any(
            item.owner_sequence != group.suppressed_sequence
            for item in removed
            if item is not None
        ):
            return None
        if "".join(item.text for item in kept if item is not None) != group.text:
            return None
        if "".join(item.text for item in removed if item is not None) != group.text:
            return None
        try:
            removed_offsets.update(
                value_offsets[item.char_index]
                for item in removed
                if item is not None
            )
        except KeyError:
            return None
        suppressed.append(
            SuppressedTextOccurrence(
                text=group.text,
                rect=group.rect,
                font=group.font,
                font_size=group.font_size,
                kept_sequence=group.kept_sequence,
                suppressed_sequence=group.suppressed_sequence,
            )
        )

    if not removed_offsets:
        return None

    output = "".join(
        character
        for index, character in enumerate(value)
        if index not in removed_offsets
    )
    remaining_to_remove = remove_word_counts.copy()
    filtered_words: list[PdfWord] = []
    for word in reversed(words):
        key = _word_key(word)
        if remaining_to_remove[key] > 0:
            remaining_to_remove[key] -= 1
            continue
        filtered_words.append(word)
    if any(remaining_to_remove.values()):
        return None

    return SuppressedAreaText(
        text=output,
        words=tuple(reversed(filtered_words)),
        suppressed=tuple(
            sorted(
                suppressed,
                key=lambda item: item.suppressed_sequence,
            )
        ),
    )


def _object_kind(object_type: int, pdfium_raw) -> str:
    known_types = (
        ("FPDF_PAGEOBJ_TEXT", "text"),
        ("FPDF_PAGEOBJ_PATH", "path"),
        ("FPDF_PAGEOBJ_IMAGE", "image"),
        ("FPDF_PAGEOBJ_SHADING", "shading"),
        ("FPDF_PAGEOBJ_FORM", "form"),
    )
    for constant_name, kind in known_types:
        constant = getattr(pdfium_raw, constant_name, None)
        if constant is not None and int(constant) == int(object_type):
            return kind
    return "unknown"


def _page_object_rect(page_object, space: PdfiumCoordinateSpace) -> PdfRect | None:
    try:
        rect = space.pdfium_to_app(PdfRect.from_value(page_object.get_bounds()))
    except Exception:
        return None
    return rect if rect.is_valid() else None


def _object_color(getter, page_object) -> tuple[int, int, int, int] | None:
    channels = tuple(ctypes.c_uint() for _ in range(4))
    if not getter(page_object, *(ctypes.byref(channel) for channel in channels)):
        return None
    return tuple(int(channel.value) for channel in channels)


def _object_has_clip(page_object, pdfium_raw) -> bool:
    clip_path = pdfium_raw.FPDFPageObj_GetClipPath(page_object)
    if _native_handle_token(clip_path) is None:
        raise ValueError("PDFium object clip-path metadata is unavailable")
    path_count = int(pdfium_raw.FPDFClipPath_CountPaths(clip_path))
    if path_count == -1:
        # PDFium returns a borrowed mutable clip-path handle for every valid
        # page object. Its CountPaths() implementation returns -1 when that
        # handle has no referenced paths, which is the normal unclipped case.
        # A failed/null handle above remains unknown and is rejected.
        return False
    if path_count < 0:
        raise ValueError("PDFium clipping metadata is unavailable")
    return True


def _native_handle_token(handle) -> int | None:
    if handle is None:
        return None
    if isinstance(handle, int):
        return handle or None
    if isinstance(handle, ctypes.c_void_p):
        return handle.value
    value = ctypes.cast(handle, ctypes.c_void_p).value
    return None if value is None else int(value)


def _object_fully_contained(
    item: NativeObjectEvidence,
    region: PdfRect,
) -> bool:
    if not _rect_contains(region, item.rect):
        return False
    return bool(item.glyphs) and all(
        _rect_contains(region, glyph.rect) for glyph in item.glyphs
    )


def _rect_contains(outer: PdfRect, inner: PdfRect | None) -> bool:
    if inner is None or not outer.is_valid() or not inner.is_valid():
        return False
    left = outer.normalized()
    right = inner.normalized()
    tolerance = _GEOMETRY_TOLERANCE
    return (
        left.x0 - tolerance <= right.x0
        and left.y0 - tolerance <= right.y0
        and right.x1 <= left.x1 + tolerance
        and right.y1 <= left.y1 + tolerance
    )


def _visible_value_mapping(
    value: str,
    page_glyphs: tuple[PageGlyphEvidence, ...],
    region: PdfRect,
) -> tuple[dict[int, int] | None, tuple[PageGlyphEvidence, ...]]:
    canonical_value = canonicalize_pdf_characters(value)
    if len(canonical_value) != len(value):
        return None, ()
    value_offsets = tuple(
        index
        for index, character in enumerate(canonical_value)
        if _is_visible_character(character)
    )
    value_characters = tuple(canonical_value[index] for index in value_offsets)

    visible_page_glyphs: list[PageGlyphEvidence] = []
    for glyph in page_glyphs:
        if glyph.rect is None:
            if _is_visible_character(glyph.text):
                return None, ()
            continue
        if glyph.rect.intersect(region) is None:
            continue
        canonical_glyph = canonicalize_pdf_characters(glyph.text)
        if len(canonical_glyph) != 1:
            return None, ()
        if _is_visible_character(canonical_glyph):
            visible_page_glyphs.append(glyph)

    page_characters = tuple(
        canonicalize_pdf_characters(glyph.text)
        for glyph in visible_page_glyphs
    )
    if page_characters != value_characters:
        return None, ()
    if len(value_offsets) != len(visible_page_glyphs):
        return None, ()

    return (
        {
            glyph.char_index: output_offset
            for glyph, output_offset in zip(visible_page_glyphs, value_offsets)
        },
        tuple(visible_page_glyphs),
    )


def _candidate_key(
    candidate: DuplicateCandidate,
) -> tuple[str, tuple[float, float, float, float]]:
    return candidate.text, candidate.rect.normalized().to_tuple()


def _word_key(
    word: PdfWord,
) -> tuple[str, tuple[float, float, float, float]]:
    return word.text, word.rect.normalized().to_tuple()


def _is_visible_character(value: str) -> bool:
    return bool(value) and not value.isspace() and not unicodedata.category(
        value
    ).startswith("C")


def _is_exact_repetition(candidate_text: str, object_text: str) -> bool:
    """Accept only a non-empty native string repeated exactly in the word."""
    return bool(candidate_text and object_text) and candidate_text == object_text * (
        len(candidate_text) // len(object_text)
    ) and len(candidate_text) % len(object_text) == 0


def _is_supported_text_object(
    item: NativeObjectEvidence,
    candidate: DuplicateCandidate,
) -> bool:
    if (
        item.kind != "text"
        or item.text is None
        or not _is_exact_repetition(candidate.text, item.text)
        or item.font_identity is None
        or not item.font_name
        or item.font_size is None
        or not math.isfinite(item.font_size)
        or item.font_size <= 0.0
        or item.matrix is None
        or len(item.matrix) != 6
        or not all(math.isfinite(value) for value in item.matrix)
        or not _valid_opaque_rgba(item.fill_rgba)
        or not _valid_opaque_rgba(item.stroke_rgba)
        or item.render_mode != 0
        or item.has_transparency
        or item.has_clip
        or item.has_marks
        or item.form_depth != 0
        or not _rectangles_equivalent(item.rect, candidate.rect)
    ):
        return False

    if not item.glyphs or len(item.glyphs) != len(item.text):
        return False
    if "".join(glyph.text for glyph in item.glyphs) != item.text:
        return False
    if len({glyph.char_index for glyph in item.glyphs}) != len(item.glyphs):
        return False
    for glyph in item.glyphs:
        if (
            not glyph.text
            or len(glyph.text) != 1
            or not glyph.rect.is_valid()
            or not all(math.isfinite(value) for value in glyph.origin)
            or not math.isfinite(glyph.angle)
        ):
            return False
    return True


def _same_visual_text_state(
    left: NativeObjectEvidence,
    right: NativeObjectEvidence,
) -> bool:
    if (
        left.text != right.text
        or left.font_identity != right.font_identity
        or left.font_name != right.font_name
        or left.fill_rgba != right.fill_rgba
        or left.stroke_rgba != right.stroke_rgba
        or left.render_mode != right.render_mode
        or left.has_transparency != right.has_transparency
        or left.has_clip != right.has_clip
        or left.has_marks != right.has_marks
        or left.form_depth != right.form_depth
        or left.font_size is None
        or right.font_size is None
        or left.matrix is None
        or right.matrix is None
        or len(left.matrix) != len(right.matrix)
        or len(left.glyphs) != len(right.glyphs)
        or left.font_size != right.font_size
        or not all(
            a == b for a, b in zip(left.matrix, right.matrix)
        )
        or not _rectangles_equivalent(left.rect, right.rect)
    ):
        return False

    return all(
        first.text == second.text
        and _rectangles_equivalent(first.rect, second.rect)
        and all(
            _close(a, b)
            for a, b in zip(first.origin, second.origin)
        )
        and _close(first.angle, second.angle)
        for first, second in zip(left.glyphs, right.glyphs)
    )


def _has_unambiguous_char_mapping(
    objects: tuple[NativeObjectEvidence, ...] | list[NativeObjectEvidence],
) -> bool:
    indices = [
        glyph.char_index
        for item in objects
        for glyph in item.glyphs
    ]
    return len(indices) == len(set(indices))


def _intervening_paint_may_overlap(
    objects: tuple[NativeObjectEvidence, ...],
    members: set[int],
    first_sequence: int,
    last_sequence: int,
    rect: PdfRect,
) -> bool:
    for item in objects:
        if not first_sequence < item.sequence < last_sequence:
            continue
        if item.sequence in members:
            continue
        if item.rect is None or not item.rect.is_valid():
            return True
        if item.rect.intersect(rect) is not None:
            return True
    return False


def _rectangles_equivalent(
    left: PdfRect | None,
    right: PdfRect | None,
) -> bool:
    if left is None or right is None:
        return False
    if not left.is_valid() or not right.is_valid():
        return False
    return all(
        _close(first, second)
        for first, second in zip(left.normalized().to_tuple(), right.normalized().to_tuple())
    )


def _close(left: float, right: float) -> bool:
    return (
        math.isfinite(left)
        and math.isfinite(right)
        and abs(left - right) <= _GEOMETRY_TOLERANCE
    )


def _valid_rgba(color: tuple[int, int, int, int] | None) -> bool:
    return color is not None and len(color) == 4 and all(
        isinstance(channel, int) and 0 <= channel <= 255
        for channel in color
    )


def _valid_opaque_rgba(color: tuple[int, int, int, int] | None) -> bool:
    return _valid_rgba(color) and color[3] == 255
