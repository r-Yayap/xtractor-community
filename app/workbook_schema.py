"""Extraction workbook contract identity (independent of application SemVer).

Version 1 was the first formally tagged contract; version 2 added
manifest-backed PDF navigation. Version 3 puts Open PDF before Folder in the
default Sheet1 order. Version 4 omits revision columns and template audit
data from Community workbooks. Older untagged workbooks remain unversioned.
See docs/RELEASING.md.
"""

WORKBOOK_SCHEMA_VERSION = 4
