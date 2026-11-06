# app/infra/excel_writer.py
from __future__ import annotations

import csv
import logging
import math
import os
from pathlib import Path
from typing import Dict, Iterable, Optional, Sequence, Tuple, Union

from openpyxl import Workbook
from openpyxl.drawing.image import Image as ExcelImage
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from app.domain.extraction_schema import (
    FINAL_BASE_ORDER,
    file_id_from_unid,
    final_area_column_indices,
    final_base_columns,
    final_base_column_index,
    final_base_value,
    final_sheet_headers,
    parse_staged_csv_row,
)
from app.domain.file_manifest import FileManifestEntry
from app.domain.ocr_value import decode_ocr_value
from app.infra.excel_output_safety import (
    literal_cell,
    sanitize_literal_value,
    write_literal_cell,
    write_trusted_formula,
)
from app.infra.suppression_audit import SUPPRESSED_TEXT_HEADERS
from app.domain.extraction_summary import SUMMARY_LABELS, summary_status
from app.version import __version__
from app.workbook_schema import WORKBOOK_SCHEMA_VERSION


logger = logging.getLogger(__name__)


def _unlink_if_same_file(path: Path, identity: tuple[int, int]) -> None:
    """Remove only the file created by this writer after a failed save."""
    try:
        current = os.stat(path)
    except FileNotFoundError:
        return
    except OSError:
        return
    if (current.st_dev, current.st_ino) != identity:
        return
    try:
        path.unlink()
    except OSError as exc:
        logger.debug(
            "Could not remove failed workbook publication (%s)",
            type(exc).__name__,
        )


def _save_workbook_exclusive(wb: Workbook, out_path: Path) -> Path:
    """Save to the exact path, rejecting any path already occupied."""
    final_out = Path(out_path)
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(os.fspath(final_out), flags, 0o666)
    identity = os.fstat(fd)
    try:
        with os.fdopen(fd, "w+b") as stream:
            fd = -1
            wb.save(stream)
            stream.flush()
    except BaseException:
        if fd != -1:
            os.close(fd)
        _unlink_if_same_file(final_out, (identity.st_dev, identity.st_ino))
        raise
    return final_out


def _raise_if_destination_occupied(out_path: Path) -> None:
    try:
        os.lstat(out_path)
    except FileNotFoundError:
        return
    raise FileExistsError(os.fspath(out_path))


FILE_MANIFEST_HEADERS = (
    "File ID",
    "SHA256",
    "Size (Bytes)",
    "Date Last Modified",
    "Modified Time (ns)",
    "Folder",
    "Filename",
    "Relative Path",
    "Absolute Path",
    "Processing Duration (ms)",
    "Status",
    "Source Changed During Extraction",
    "Content Group",
    "Current Relationship",
    "Related File(s)",
    "Warning / Error",
)

# Backward-compatible default Sheet1 metadata order.
#
# Runtime extraction output must use the per-request base_order argument.
# This alias exists only for older integrations that imported
# OUTPUT_BASE_ORDER from this module.
OUTPUT_BASE_ORDER = list(FINAL_BASE_ORDER)


def staged_area_image_name(
    unid_prefix: object,
    filename: object,
    page_number: object,
    area_index: int,
) -> str:
    return (
        f"{unid_prefix}_{filename}_page{page_number}_area{int(area_index)}.png"
    )

def _clean_cell_value(val):
    """Apply the shared XML-safe literal normalization to a value."""
    return sanitize_literal_value(val)


def _csv_has_errors(
    combined_csv: Path,
    area_headers: Sequence[str] = (),
) -> bool:
    with open(combined_csv, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header:
            return False
        for row in reader:
            parsed = parse_staged_csv_row(row, header, area_headers)
            if str(parsed.file_values.get("Error", "")).strip():
                return True
    return False


MetricsValue = Union[str, int, float]

_AUDIT_INTEGER_COLUMNS = {3, 4, 5, 14, 15, 18}
_AUDIT_FLOAT_COLUMNS = {8, 9, 10, 11, 13}


def _validate_suppressed_text_csv(suppressed_csv: Optional[Path]) -> bool:
    if not suppressed_csv or not suppressed_csv.is_file():
        return False

    expected_header = list(SUPPRESSED_TEXT_HEADERS)
    with suppressed_csv.open(newline="", encoding="utf-8") as stream:
        reader = csv.reader(stream)
        header = next(reader, None)
        if header is None:
            raise ValueError(
                f"SuppressedText row 1 header: expected {len(expected_header)} "
                "columns, but the file is empty"
            )
        if header != expected_header:
            mismatch_index = next(
                (
                    index
                    for index in range(max(len(header), len(expected_header)))
                    if (header[index] if index < len(header) else None)
                    != (expected_header[index] if index < len(expected_header) else None)
                ),
                0,
            )
            expected = (
                repr(expected_header[mismatch_index])
                if mismatch_index < len(expected_header)
                else "end of header"
            )
            actual = (
                repr(header[mismatch_index])
                if mismatch_index < len(header)
                else "end of header"
            )
            raise ValueError(
                "SuppressedText row 1 header, "
                f"column {mismatch_index + 1}: expected {expected}, got {actual}"
            )

        has_rows = False
        for row_number, row in enumerate(reader, start=2):
            if len(row) != len(SUPPRESSED_TEXT_HEADERS):
                mismatch_column = min(len(row), len(SUPPRESSED_TEXT_HEADERS)) + 1
                raise ValueError(
                    f"SuppressedText row {row_number}, column {mismatch_column}: "
                    f"expected {len(SUPPRESSED_TEXT_HEADERS)} fields, got {len(row)}"
                )
            for index in sorted(_AUDIT_INTEGER_COLUMNS | _AUDIT_FLOAT_COLUMNS):
                column_name = SUPPRESSED_TEXT_HEADERS[index]
                expected_type = "integer" if index in _AUDIT_INTEGER_COLUMNS else "number"
                try:
                    converted = (
                        int(row[index])
                        if index in _AUDIT_INTEGER_COLUMNS
                        else float(row[index])
                    )
                    if isinstance(converted, float) and not math.isfinite(converted):
                        raise ValueError
                except (TypeError, ValueError):
                    raise ValueError(
                        f"SuppressedText row {row_number}, {column_name} "
                        f"(column {index + 1}): expected {expected_type}, got {row[index]!r}"
                    ) from None
            has_rows = True
    return has_rows


def _append_bold_header(ws, values: Sequence[str]) -> None:
    header_font = Font(bold=True)
    cells = [literal_cell(ws, value) for value in values]
    if ws.parent.write_only:
        for cell in cells:
            cell.font = header_font
        ws.append(cells)
        return

    ws.append(cells)
    for cell in ws[1]:
        cell.font = header_font


def _typed_audit_row(row: Sequence[str]) -> list[object]:
    values: list[object] = []
    for index, value in enumerate(row):
        clean_value = _clean_cell_value(value)
        if index in _AUDIT_INTEGER_COLUMNS:
            clean_value = int(value)
        elif index in _AUDIT_FLOAT_COLUMNS:
            clean_value = float(value)
        values.append(clean_value)
    return values


def _add_suppressed_text_sheet(
    wb: Workbook,
    suppressed_csv: Optional[Path],
    has_rows: bool,
) -> int:
    ws = wb.create_sheet("SuppressedText")
    if has_rows:
        ws.sheet_properties.tabColor = "FFFF0000"
    ws.freeze_panes = "A2"
    widths = (
        16, 28, 32, 10, 16, 12, 24, 45, 12, 12, 12, 12, 18, 12, 16, 20,
        24, 24, 10,
    )
    for index, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(index)].width = width
    _append_bold_header(ws, SUPPRESSED_TEXT_HEADERS)

    row_count = 0
    if has_rows and suppressed_csv and suppressed_csv.is_file():
        with suppressed_csv.open(newline="", encoding="utf-8") as stream:
            reader = csv.reader(stream)
            next(reader, None)
            for row in reader:
                ws.append([
                    literal_cell(ws, value)
                    for value in _typed_audit_row(row)
                ])
                row_count += 1

    return row_count


def _add_metrics_sheet(
    wb: Workbook,
    metrics: Optional[Sequence[Tuple[str, MetricsValue]]],
) -> None:
    # Reserved provenance comes from the authorities, never optional callers.
    # Append it so existing non-version metrics keep their values and order.
    metadata = {
        "Xtractor Version": __version__,
        "Workbook Schema Version": WORKBOOK_SCHEMA_VERSION,
    }
    metrics = [
        (label, value) for label, value in (metrics or ())
        if _clean_cell_value(label) not in metadata
    ]
    metrics.extend(metadata.items())

    ws = wb.create_sheet("ExtractionMetrics")
    header_font = Font(bold=True)

    if wb.write_only:
        header = [literal_cell(ws, "Metric"), literal_cell(ws, "Value")]
        header[0].font = header_font
        header[1].font = header_font
        ws.append(header)
        for label, value in metrics:
            label_cell = literal_cell(ws, label)
            value_cell = literal_cell(ws, value)
            fill = _summary_metric_fill(label, value)
            if fill is not None:
                value_cell.fill = fill
            ws.append([label_cell, value_cell])
        return

    ws.append([literal_cell(ws, "Metric"), literal_cell(ws, "Value")])
    ws["A1"].font = header_font
    ws["B1"].font = header_font
    for label, value in metrics:
        ws.append([literal_cell(ws, label), literal_cell(ws, value)])
        fill = _summary_metric_fill(label, value)
        if fill is not None:
            ws.cell(ws.max_row, 2).fill = fill
    ws.column_dimensions["A"].width = 32
    ws.column_dimensions["B"].width = 50


_SUMMARY_FILL_COLORS = {
    "success": "FFC6EFCE",
    "warning": "FFFFEB9C",
    "info": "FFDDEBF7",
    "error": "FFFFC7CE",
    "muted": "FFE7E6E6",
}


def _summary_metric_fill(label: object, value: object) -> PatternFill | None:
    clean_label = str(label)
    if clean_label not in SUMMARY_LABELS:
        return None
    color = _SUMMARY_FILL_COLORS[summary_status(clean_label, str(value))]
    return PatternFill(fill_type="solid", fgColor=color)


_MANIFEST_WIDTHS = (14, 68, 14, 22, 22, 24, 36, 60, 80, 24, 16, 22, 14, 38, 60, 60)


def _manifest_status_fill(entry: FileManifestEntry) -> PatternFill | None:
    if entry.status.casefold() not in {"success", "ok", "completed"}:
        return PatternFill(fill_type="solid", fgColor="FFFFC7CE")
    if entry.source_changed_during_extraction or entry.warning_error:
        return PatternFill(fill_type="solid", fgColor="FFFFEB9C")
    if entry.content_group and entry.content_group != "Unique":
        return PatternFill(fill_type="solid", fgColor="FFDDEBF7")
    return None


def _file_manifest_row(entry: FileManifestEntry) -> list[object]:
    warnings = "; ".join(
        value for value in (entry.extraction_error, entry.warning_error) if value
    )
    return [
        entry.file_id,
        entry.sha256,
        entry.size_bytes,
        entry.modified_display,
        str(entry.modified_time_ns) if entry.modified_time_ns else "",
        entry.folder,
        entry.filename,
        entry.relative_path,
        entry.absolute_path,
        entry.duration_ms,
        entry.status,
        entry.source_changed_during_extraction,
        entry.content_group,
        entry.current_relationship,
        "; ".join(entry.related_files),
        warnings,
    ]


def _add_file_manifest_sheet(
    wb: Workbook,
    file_manifest: Sequence[FileManifestEntry],
) -> int:
    ws = wb.create_sheet("File Manifest")
    ws.freeze_panes = "A2"
    for index, width in enumerate(_MANIFEST_WIDTHS, start=1):
        ws.column_dimensions[get_column_letter(index)].width = width
    _append_bold_header(ws, FILE_MANIFEST_HEADERS)
    status_col_idx0 = FILE_MANIFEST_HEADERS.index("Status")

    for entry in file_manifest:
        row = [_clean_cell_value(value) for value in _file_manifest_row(entry)]
        fill = _manifest_status_fill(entry)
        cells = [literal_cell(ws, value) for value in row]
        if fill is not None and wb.write_only:
            cells[status_col_idx0].fill = fill
        ws.append(cells)
        if fill is not None and not wb.write_only:
            ws.cell(ws.max_row, status_col_idx0 + 1).fill = fill
    return len(file_manifest)


def _manifest_row_by_file_id(
    file_manifest: Sequence[FileManifestEntry],
) -> dict[str, int]:
    """Build the one-time identity-to-manifest-row publication map."""

    rows: dict[str, int] = {}
    for row_number, entry in enumerate(file_manifest, start=2):
        file_id = str(entry.file_id)
        if file_id in rows:
            raise ValueError(f"Duplicate File Manifest File ID: {file_id}")
        rows[file_id] = row_number
    return rows


def _excel_sheet_reference(
    sheet_name: str,
    column_letter: str,
    row_number: int,
) -> str:
    escaped_name = str(sheet_name).replace("'", "''")
    return f"'{escaped_name}'!${column_letter}${int(row_number)}"


def _open_pdf_formula(manifest_column_letter: str, manifest_row: int) -> str:
    reference = _excel_sheet_reference(
        "File Manifest", manifest_column_letter, manifest_row
    )
    return f'=HYPERLINK({reference},"Open PDF")'


def write_from_csv(
    combined_csv: Path,
    out_path: Path,
    temp_image_folder: Path,
    unique_headers_mapping: Dict[int, str],
    needs_images: bool,
    pdf_root: Path,
    metrics: Optional[Sequence[Tuple[str, MetricsValue]]] = None,
    suppressed_csv: Optional[Path] = None,
    file_manifest: Optional[Sequence[FileManifestEntry]] = None,
    base_order: Sequence[object] | None = None,
) -> Path:
    """Stream canonical staged rows into Sheet1 and file-level workbook tabs."""
    _raise_if_destination_occupied(Path(out_path))
    suppressed_has_rows = _validate_suppressed_text_csv(suppressed_csv)
    area_headers = [
        unique_headers_mapping[index]
        for index in range(len(unique_headers_mapping))
    ]
    include_error_column = _csv_has_errors(combined_csv, area_headers)
    output_base_order = final_base_columns(
        include_error=include_error_column,
        base_order=base_order,
    )
    headers = final_sheet_headers(
        area_headers,
        include_error=include_error_column,
        base_order=base_order,
    )

    manifest_entries = tuple(file_manifest or ())
    manifest_row_by_file_id = _manifest_row_by_file_id(manifest_entries)
    manifest_absolute_path_col_letter = get_column_letter(
        FILE_MANIFEST_HEADERS.index("Absolute Path") + 1
    )

    wb = Workbook(write_only=not needs_images)
    ws = wb.create_sheet("Sheet1") if wb.write_only else wb.active
    if not wb.write_only:
        ws.title = "Sheet1"
    ws.append([literal_cell(ws, value) for value in headers])

    for required in ("Folder", "Filename", "Page No"):
        if required not in output_base_order:
            raise ValueError(f"Required column missing in headers: {required}")
    filename_col_idx0 = final_base_column_index(
        "Filename",
        include_error=include_error_column,
        base_order=base_order,
    )
    folder_col_idx0 = final_base_column_index(
        "Folder",
        include_error=include_error_column,
        base_order=base_order,
    )
    page_no_idx0 = final_base_column_index(
        "Page No",
        include_error=include_error_column,
        base_order=base_order,
    )
    open_pdf_col_idx0 = final_base_column_index(
        "Open PDF",
        include_error=include_error_column,
        base_order=base_order,
    )
    unid_col_idx0 = final_base_column_index(
        "UNID",
        include_error=include_error_column,
        base_order=base_order,
    )
    area_col_idxs0 = list(
        final_area_column_indices(
            len(area_headers),
            include_error=include_error_column,
            base_order=base_order,
        )
    )
    ocr_font = Font(color="FF3300")
    image_embedding_warning_logged = False

    with Path(combined_csv).open("r", encoding="utf-8") as stream:
        reader = csv.reader(stream)
        input_headers = next(reader, None) or []
        for row in reader:
            parsed = parse_staged_csv_row(row, input_headers, area_headers)
            file_values = parsed.file_values
            row_values = [
                _clean_cell_value(final_base_value(name, file_values))
                for name in output_base_order
            ]
            row_values.extend(_clean_cell_value(value) for value in parsed.area_values)
            if needs_images:
                ws.append([literal_cell(ws, value) for value in row_values])
                row_number = ws.max_row
                folder_val = row_values[folder_col_idx0]
                filename_val = row_values[filename_col_idx0]
                file_id = file_id_from_unid(row_values[unid_col_idx0])
                manifest_row = manifest_row_by_file_id.get(file_id)
                if manifest_row is not None:
                    cell = ws.cell(row=row_number, column=open_pdf_col_idx0 + 1)
                    write_trusted_formula(
                        cell,
                        _open_pdf_formula(
                            manifest_absolute_path_col_letter, manifest_row
                        ),
                    )
                    cell.font = Font(color="0000FF", underline="single")
                page_no_val = row_values[page_no_idx0]
                for area_index, col_idx0 in enumerate(area_col_idxs0):
                    cell = ws.cell(row=row_number, column=col_idx0 + 1)
                    visible_value, is_ocr = decode_ocr_value(cell.value)
                    if is_ocr:
                        cell.font = ocr_font
                        write_literal_cell(
                            cell,
                            visible_value.strip()
                            if isinstance(visible_value, str)
                            else visible_value,
                        )
                    try:
                        if folder_val and filename_val and page_no_val:
                            unid_prefix = file_id_from_unid(row_values[unid_col_idx0])
                            image_filename = staged_area_image_name(
                                unid_prefix,
                                filename_val,
                                page_no_val,
                                area_index,
                            )
                            img_path = Path(temp_image_folder) / image_filename
                            if img_path.exists():
                                image = ExcelImage(str(img_path))
                                image.anchor = f"{get_column_letter(col_idx0 + 1)}{row_number}"
                                ws.add_image(image)
                    except Exception as exc:
                        if not image_embedding_warning_logged:
                            logger.warning(
                                "Optional extraction image embedding failed (%s)",
                                type(exc).__name__,
                            )
                            image_embedding_warning_logged = True
            else:
                folder_val = row_values[folder_col_idx0]
                filename_val = row_values[filename_col_idx0]
                file_id = file_id_from_unid(row_values[unid_col_idx0])
                manifest_row = manifest_row_by_file_id.get(file_id)
                row_cells = []
                for index, value in enumerate(row_values):
                    cell = literal_cell(ws, value)
                    if index == open_pdf_col_idx0 and manifest_row is not None:
                        cell.font = Font(color="0000FF", underline="single")
                        write_trusted_formula(
                            cell,
                            _open_pdf_formula(
                                manifest_absolute_path_col_letter, manifest_row
                            ),
                        )
                    if index in area_col_idxs0:
                        visible_value, is_ocr = decode_ocr_value(cell.value)
                        if is_ocr:
                            write_literal_cell(
                                cell,
                                visible_value.strip()
                                if isinstance(visible_value, str)
                                else visible_value,
                            )
                            cell.font = ocr_font
                    row_cells.append(cell)
                ws.append(row_cells)

    _add_file_manifest_sheet(wb, manifest_entries)
    _add_suppressed_text_sheet(wb, suppressed_csv, suppressed_has_rows)
    _add_metrics_sheet(wb, metrics)
    return _save_workbook_exclusive(wb, Path(out_path))
