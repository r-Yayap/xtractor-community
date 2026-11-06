# Xtractor Community

Xtractor Community extracts text from areas you mark on a PDF and writes the
results for a batch of PDFs to Excel. This is the Community source release
for Windows. The first Community version is `2.1.0-community.1`.

## Run from source

Use Python 3.12 on Windows.

```powershell
py -3.12 -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt
./.venv/Scripts/python.exe main.py
```

For scanned PDFs, install [Tesseract](https://tesseract-ocr.github.io/tessdoc/Installation.html)
with its English language data. Xtractor checks common Windows installation
locations and `PATH`. For a custom installation, set `XTRACTOR_TESSERACT_CMD`
to the executable and `XTRACTOR_TESSDATA_DIR` to the folder containing
`eng.traineddata`.

## Extract to Excel

1. Choose a PDF folder or drop PDF files into the workspace.
2. Open a representative PDF and draw a box around each field.
3. Enable **Use OCR for scanned PDFs** when the text is part of an image.
4. Choose an Excel output file and click **Extract to Excel**.

The workbook includes one column per area, a file manifest, extraction metrics,
and a duplicate-text audit. Source PDF links use absolute paths.

See the [Quick Start](docs/QUICK_START.md), [User Guide](docs/USER_GUIDE.md),
and [Troubleshooting](docs/TROUBLESHOOTING.md). Open offline Help with **F1**.

## Tests

```powershell
./.venv/Scripts/python.exe -m pip install pytest
./.venv/Scripts/python.exe -m pytest -q
```

The scanned-PDF test uses a local Tesseract installation when available.

## License

The Xtractor Community code in this repository is available under the
[MIT License](LICENSE). Third-party components have their own terms.

## Third-party dependencies

Python libraries and the optional OCR runtime are installed separately. See the
[Third-Party Notices](THIRD_PARTY_NOTICES.md).
