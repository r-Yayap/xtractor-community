"""Explicit trust boundaries for values written to Excel workbooks."""

from __future__ import annotations

from openpyxl.cell import WriteOnlyCell
from openpyxl.cell.cell import Cell, ILLEGAL_CHARACTERS_RE


def sanitize_literal_value(value: object) -> object:
    """Remove characters rejected by XLSX XML while preserving other types."""
    if isinstance(value, str):
        return ILLEGAL_CHARACTERS_RE.sub("", value)
    return value


def write_literal_cell(cell: Cell, value: object) -> None:
    """Write visible data and force text values to remain literal XLSX strings."""
    clean_value = sanitize_literal_value(value)
    cell.value = clean_value
    if isinstance(clean_value, str):
        cell.data_type = "s"


def literal_cell(worksheet, value: object) -> Cell:
    """Create an appendable cell whose text is always stored literally."""
    cell = WriteOnlyCell(worksheet)
    write_literal_cell(cell, value)
    return cell


def sanitize_dataframe_values(dataframe):
    """Clean XML-illegal text before writing cells with openpyxl."""
    sanitized = dataframe.copy()
    sanitized.columns = [
        sanitize_literal_value(label) for label in sanitized.columns
    ]
    return sanitized.apply(
        lambda column: column.map(sanitize_literal_value)
    )


def literalize_worksheet(worksheet) -> None:
    """Convert every text-valued cell, including imported formulas, to literal text."""
    for row in worksheet.iter_rows():
        for cell in row:
            if isinstance(cell.value, str):
                write_literal_cell(cell, cell.value)


def literalize_workbook(workbook) -> None:
    """Literalize all text in a workbook that contains no app-owned formulas."""
    for worksheet in workbook.worksheets:
        literalize_worksheet(worksheet)


def write_trusted_formula(cell: Cell, formula: str) -> None:
    """Write a formula deliberately constructed by Xtractor application code."""
    if not isinstance(formula, str) or not formula.startswith("="):
        raise ValueError("Trusted Excel formula must begin with '='.")
    cell.value = formula
    cell.data_type = "f"
