# Logging and diagnostics

This guide describes the local application log and the information it is designed to retain. Xtractor does not upload logs or send telemetry.

## Log location

Unless `XTRACTOR_LOG_DIR` is set, Xtractor stores its log in the per-user Xtractor log directory:

- Windows: `%LOCALAPPDATA%\Xtractor\Logs\app.log`
- macOS: `~/Library/Application Support/Xtractor/Logs/app.log`
- Linux: `$XDG_CONFIG_HOME/xtractor/Logs/app.log`, or `~/.config/xtractor/Logs/app.log` when `XDG_CONFIG_HOME` is unset

`XTRACTOR_LOG_DIR` can point to a different directory. An absolute path is recommended. Relative paths are resolved from the application's current working directory. This override changes the storage location, while the logging privacy and worker-process rules still apply.

## Log contents

The app log records timestamps, severity, application module and line, process name, and bounded operational events. Depending on the operation, this can include page numbers, status or diagnostic codes, exception class names, counts, and elapsed times.

The persistent app log is designed to exclude source PDF names and paths, extracted PDF or OCR text, workbook cell values, raw exception messages, and exception tracebacks. PDF backend warnings use page and exception-class information without source names. Tesseract stdout and stderr are captured for in-memory parsing and are not copied into the app log. Unreviewed third-party logger records are not written by the app file handler.

`XTRACTOR_LOG_LEVEL=DEBUG` can enable additional operational events, but it does not enable document-content diagnostics or tracebacks. Do not put customer data, credentials, or tokens in environment-variable values that may be copied into support material.

## Rotation and worker processes

The app keeps `app.log` plus up to three numbered backups (`app.log.1` through `app.log.3`). Rotation starts at 2,000,000 bytes per file, so the usual total is about 8 MB. This is size-based retention, not a time-based deletion policy; older files remain until later log activity rotates them. A single unusually large record can exceed the configured size before the next rollover.

Only the main application process writes the shared log. Spawned or forked workers do not attach an independent rotating file handler, which avoids competing rotations. Worker-specific log records are not persisted; the parent process records the safe outcome it receives.

## If the log cannot be opened

File logging is best-effort. If the per-user directory or file cannot be created or written, Xtractor continues with a no-output handler and does not redirect application records to the console. A startup error may still show the intended log location, marked as available only if the file was written. Import failures that happen before logging setup completes may not appear in the log.

## Support steps

1. Note the Xtractor version, approximate time, action, and any diagnostic code shown by the app.
2. Open `app.log` in the location above and look for events around that time. The numbered backups contain older entries.
3. Review the file before sharing it. The app log is local and is not uploaded automatically; share it only through a support channel you choose.
4. If no log file exists, check that the current user can create and modify files under the configured log directory. Remove `XTRACTOR_LOG_DIR` or point it to a writable per-user directory if necessary.
5. Delete the log and its backups when they are no longer needed. Xtractor does not apply a calendar-age retention policy.
