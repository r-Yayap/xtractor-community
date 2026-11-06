# Xtractor Community Support

## Information to include

Share only what is relevant to the issue:

- **Xtractor version.** It appears in the application window title. The version is also recorded in a completed workbook's **ExtractionMetrics** sheet.
- **Windows version.** Find it under Windows **Settings > System > About**.
- **What happened.** Give the exact steps you took and the action that was in progress when the problem appeared. Copy the displayed error title and message; include a diagnostic code if the application shows one.
- **Extraction setup.** Include the selected PDF count, the number of marked areas, and whether OCR was enabled.
- **Source context.** Say whether the PDFs were on a local disk, network location, or cloud-synchronized folder. Do not include the folder path, share name, computer name, or project name.
- **Result details.** If extraction completed, include the relevant completion counts. Where useful, report what the **File Manifest** or **SuppressedText** sheet shows. Do not send a workbook without checking its contents and source paths first.
- **Log, when useful.** Include the application log for startup problems or unexpected failures. See [Logging and diagnostics](logging-and-diagnostics.md) for its location and contents. Review it before sharing.

To find the local logs, open the Help menu with the **?** button in the title bar or press **F1**, then choose **Open Logs Folder**. Xtractor does not upload logs; review them before sharing.

If support needs an example, use only a sanitized sample PDF that your organization permits you to share. A sample should not contain real customer, project, drawing, or personal information.

## Protect project information

Do not send confidential project documents unless you are authorized to do so. Extraction workbooks can contain extracted document values and absolute source paths in **File Manifest**. Review and sanitize a copy before sharing it, including every worksheet and link target. Do not assume that a hidden worksheet or a log is safe to distribute without review.

For common corrective steps, start with [Troubleshooting](TROUBLESHOOTING.md). For the full product workflow, see the [User Guide](USER_GUIDE.md).
