# Xtractor Community User Guide

Use the [Quick Start](QUICK_START.md) for your first extraction.

## Select and preview PDFs

Choose a folder or drop individual PDFs into the workspace. Xtractor scans folders and their subfolders. Check or uncheck files in the tree to decide which PDFs go into Excel. Double-click a file to preview it. Use the page controls to move through a multipage PDF and the slider to zoom.

## Draw and adjust extraction areas

On a representative PDF, drag a box around each field you want in Excel. Use **Extract Text** to return to drawing mode. The sidebar lists the numbered areas in workbook column order. Select an area in the list to highlight its box.

- Use **Resize** to drag a box or move its edges.
- Use **Delete** or the box's context menu to remove one area.
- Use **Clear areas** to remove all boxes.
- Press **Ctrl+Z** to undo a canvas edit and **Ctrl+Y** to redo it.

Areas are held in the current session. Xtractor scales their positions from the page where you drew the first box to the dimensions of each target page. Choose PDFs with similar layouts and check the boxes on a few representative pages before extracting.

## Run an extraction

For searchable PDFs, leave the OCR checkbox clear. Select **Use OCR for scanned PDFs** to apply OCR to every selected area. Choose a writable .xlsx destination and click **Extract to Excel**. If a workbook already exists with that name, Xtractor preserves it and allocates a new filename.

## Read the workbook

- **Sheet1** contains the file and page information, a link to the source PDF, and one column per extraction area. OCR values are marked in red. An **Error** column appears when a file could not be processed.
- **File Manifest** contains source paths, file identity, and extraction status.
- **SuppressedText** records duplicate or overlapping text that Xtractor omitted.
- **ExtractionMetrics** records processing counts, timing, and version information.

The workbook stores absolute paths for its PDF links. Review these paths and the extracted text before sharing a workbook.

## Get help

Press **F1** or click **?** in the title bar. See [Troubleshooting](TROUBLESHOOTING.md) or [Support](SUPPORT.md).
