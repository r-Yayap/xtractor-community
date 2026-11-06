# Xtractor Community Troubleshooting Guide

## No PDFs appear

Choose a folder containing PDFs or drop one or more PDF files into the workspace. Allow the scan to finish. Check whether the files are in a readable folder or on a connected drive.

## Extract to Excel is disabled

Wait for the PDF scan to finish, check at least one PDF, and draw at least one area. Enter an .xlsx output filename in an existing writable folder. The status below the path explains output errors.

## A box misses its field on other files

Preview those files and compare their layouts with the page where you drew the areas. Use **Resize** to adjust the boxes or extract PDFs with a different layout as a separate batch.

## Text is blank or inaccurate

Check that the box fully encloses the text and excludes adjacent fields. If you cannot select the text in a PDF reader, try **Use OCR for scanned PDFs**. OCR accuracy depends on scan quality and legibility. Compare several workbook rows with the originals.

## OCR reports an error

Install Tesseract and its English language data. Make Tesseract available on
`PATH` or set `XTRACTOR_TESSERACT_CMD` to the executable and
`XTRACTOR_TESSDATA_DIR` to the directory containing `eng.traineddata`. For an application startup or OCR runtime error, use **? → Open Logs Folder** and follow the [Support Guide](SUPPORT.md).

## The Excel workbook reports an error

Look at the **Error** column and **File Manifest** to identify affected files. Check whether those PDFs open in a PDF reader. If an output filename was already in use, use the path shown in the completion dialog to locate the new workbook.

## The Open PDF link fails

The workbook link points to the source path at extraction time. A moved file or a disconnected drive can make that path unavailable. Check the **File Manifest** for the recorded path.

## Cancelling takes time

After you click **Cancel**, allow the current work to stop and the application to finish cleaning up.
