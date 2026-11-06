from contextlib import closing
import os
from pathlib import Path
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from openpyxl import load_workbook
from PIL import Image, ImageDraw, ImageFont
from PySide6 import QtCore, QtTest, QtWidgets

from app.common.help_resources import HELP_RESOURCES, resolve_help_resource
from app.common.html_documents import read_html_file
from app.domain.area_coordinates import CoordinateBasis
from app.domain.models import AreaSpec, ExtractionRequest, OcrSettings
from app.infra.ocr.errors import OcrSubsystemError
from app.infra.ocr.tesseract.runtime import discover_runtime
from app.services.extraction_service import ExtractionService
from app.ui.gui import XtractorGUI


def _searchable_pdf(path: Path, label: str, scale: int) -> None:
    stream = f"BT /F1 {11 * scale} Tf {20 * scale} {75 * scale} Td ({label}) Tj ET".encode("ascii")
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {200 * scale} {100 * scale}] "
        f"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>".encode("ascii"),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"\nendstream",
    )
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, body in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf.extend(f"{index} 0 obj\n".encode("ascii") + body + b"\nendobj\n")
    xref = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii"))
    for offset in offsets:
        pdf.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii"))
    path.write_bytes(pdf)


def test_workspace_draws_and_exports_text_from_pdfs(tmp_path, monkeypatch):
    application = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = XtractorGUI()
    try:
        first = tmp_path / "first.pdf"
        second = tmp_path / "second.pdf"
        _searchable_pdf(first, "Alpha", 1)
        _searchable_pdf(second, "Beta", 2)
        output = tmp_path / "results.xlsx"

        window.show()
        window.drop_pdf_folder([str(first), str(second)])
        window.output_path_entry.setText(str(output))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if window.pdf_viewer.has_document and window.output_readiness.state.probe_result:
                break
            QtTest.QTest.qWait(20)
        assert window.pdf_viewer.has_document
        assert not window.extract_button.isEnabled()

        view = window.pdf_viewer.view
        start = view.mapFromScene(QtCore.QPointF(20, 20))
        end = view.mapFromScene(QtCore.QPointF(240, 80))
        QtTest.QTest.mousePress(view.viewport(), QtCore.Qt.LeftButton, pos=start)
        QtTest.QTest.mouseMove(view.viewport(), end)
        QtTest.QTest.mouseRelease(view.viewport(), QtCore.Qt.LeftButton, pos=end)
        assert window.areas_tree.item(0).text() == "Rectangle 1"
        assert window.extract_button.isEnabled()

        completed = []
        failures = []
        monkeypatch.setattr(window, "_handle_successful_extraction", lambda path, _: completed.append(path))
        monkeypatch.setattr(window, "_show_extraction_preparation_failure", failures.append)
        monkeypatch.setattr(window, "_show_unsuccessful_extraction", lambda state, _: failures.append(state))
        window.extract_button.click()

        assert not failures
        assert completed == [output]
        with closing(load_workbook(output, read_only=True)) as workbook:
            assert set(workbook.sheetnames) == {"Sheet1", "File Manifest", "SuppressedText", "ExtractionMetrics"}
            header, *rows = workbook["Sheet1"].values
            assert header[-1] == "Rectangle 1"
            assert sorted(row[-1] for row in rows) == ["Alpha", "Beta"]
    finally:
        window.close()
        application.processEvents()


def test_scanned_pdf_exports_ocr_text(tmp_path):
    try:
        discover_runtime()
    except OcrSubsystemError as exc:
        pytest.skip(f"Tesseract and English data are required for this test ({exc.diagnostic.code.value})")

    pdf = tmp_path / "scan.pdf"
    with Image.new("RGB", (440, 160), "white") as image:
        ImageDraw.Draw(image).text(
            (20, 35), "DRAWING 123", fill="black", font=ImageFont.load_default(size=48)
        )
        image.save(pdf, format="PDF")

    request = ExtractionRequest(
        pdf_paths=[pdf],
        output_excel=tmp_path / "scan.xlsx",
        areas=[AreaSpec.create("Rectangle 1", (0, 0, 440, 160))],
        ocr=OcrSettings(mode="OCR-All", dpi=150, enabled=True),
        coordinate_basis=CoordinateBasis(440, 160),
    )
    result = ExtractionService().extract(request)

    assert result is not None
    with closing(load_workbook(result.output_path, read_only=True)) as workbook:
        _, row = workbook["Sheet1"].values
        assert row[-1] == "DRAWING 123"


def test_offline_help_contains_all_allowed_pages():
    for resource in HELP_RESOURCES:
        assert read_html_file(resolve_help_resource(resource.resource_id))
