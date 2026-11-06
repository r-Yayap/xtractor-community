from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
import re
import os
import stat
from urllib.parse import unquote, urlsplit

from app.common.packaging import packaged_root
from app.common.html_documents import html_path
import posixpath


_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
_INERT_EXTERNAL_SCHEMES = {"http", "https", "mailto", "tel"}


class HelpResourceError(ValueError):
    """A Help path or link is outside the local resource contract."""


@dataclass(frozen=True, slots=True)
class HelpResource:
    resource_id: str
    title: str
    source_path: str
    package_path: str
    show_in_menu: bool = False


@dataclass(frozen=True, slots=True)
class ResolvedHelpLink:
    resource: HelpResource
    fragment: str = ""


# Canonical editable text stays separate from the generated reader resource.
HELP_RESOURCES: tuple[HelpResource, ...] = (
    HelpResource(
        "quick_start",
        "Quick Start",
        "docs/QUICK_START.md",
        "docs/QUICK_START.md",
        True,
    ),
    HelpResource(
        "user_guide",
        "User Guide",
        "docs/USER_GUIDE.md",
        "docs/USER_GUIDE.md",
        True,
    ),
    HelpResource(
        "troubleshooting",
        "Troubleshooting",
        "docs/TROUBLESHOOTING.md",
        "docs/TROUBLESHOOTING.md",
        True,
    ),
    HelpResource(
        "support",
        "Support",
        "docs/SUPPORT.md",
        "docs/SUPPORT.md",
        True,
    ),
    HelpResource("software_license", "MIT License", "LICENSE", "LICENSE", True),
    HelpResource("third_party_notices", "Third-Party Notices", "THIRD_PARTY_NOTICES.md", "THIRD_PARTY_NOTICES.md", True),
    HelpResource(
        "logging_and_diagnostics",
        "Logging and diagnostics",
        "docs/logging-and-diagnostics.md",
        "docs/logging-and-diagnostics.md",
    ),
)

HELP_RESOURCES = (
    HelpResource("help_home", "Documentation Home", "docs/HELP.md", html_path("docs/HELP.md"), True),
    *(replace(resource, package_path=html_path(resource.source_path)) for resource in HELP_RESOURCES),
)

_RESOURCE_BY_ID = {resource.resource_id: resource for resource in HELP_RESOURCES}
_RESOURCE_BY_PACKAGE_PATH = {
    resource.package_path: resource for resource in HELP_RESOURCES
}


def _safe_relative_parts(value: object) -> tuple[str, ...]:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or "\\" in value
        or value.startswith("/")
        or _DRIVE_PREFIX.match(value)
    ):
        raise HelpResourceError("Help path is not repository-relative.")
    parts = tuple(value.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise HelpResourceError("Help path contains an unsafe component.")
    return parts


def get_help_resource(resource_id: str) -> HelpResource:
    """Return one allowlisted Help resource by stable logical ID."""

    try:
        return _RESOURCE_BY_ID[resource_id]
    except (KeyError, TypeError):
        raise KeyError("Unknown Help resource.") from None


def _contained_path(root: Path, relative_path: str) -> Path:
    root_path = Path(os.path.abspath(root))
    parts = _safe_relative_parts(relative_path)
    candidates = [*reversed((root_path, *root_path.parents))]
    candidates.extend(root_path.joinpath(*parts[:index]) for index in range(1, len(parts) + 1))
    for path in candidates:
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise HelpResourceError("Help resource contains an unsafe link.")
    candidate = root_path.joinpath(*parts).resolve()
    try:
        candidate.relative_to(root_path)
    except ValueError as exc:
        raise HelpResourceError("Help resource escapes its package root.") from exc
    return candidate


def resolve_help_resource(resource_id: str, root: Path | None = None) -> Path:
    """Resolve an allowlisted file using the source or frozen bundle root."""

    resource = get_help_resource(resource_id)
    package_root = Path(root) if root is not None else packaged_root()
    return _contained_path(package_root, resource.package_path)


def resolve_help_link(
    resource_id: str,
    target: str,
) -> ResolvedHelpLink | None:
    """Resolve one allowlisted HTML link; external links remain inert natively."""

    current = get_help_resource(resource_id)
    if (
        not isinstance(target, str)
        or not target
        or "\x00" in target
        or "\\" in target
    ):
        raise HelpResourceError("Help link is malformed.")
    try:
        parsed = urlsplit(target)
    except ValueError as exc:
        raise HelpResourceError("Help link is malformed.") from exc

    scheme = parsed.scheme.lower()
    if scheme in _INERT_EXTERNAL_SCHEMES:
        return None
    if scheme or parsed.netloc or parsed.query:
        raise HelpResourceError("Help link uses an unsupported target.")

    decoded_path = unquote(parsed.path)
    if not decoded_path:
        if parsed.fragment:
            return ResolvedHelpLink(current, unquote(parsed.fragment))
        return None
    if (
        decoded_path.startswith("/")
        or _DRIVE_PREFIX.match(decoded_path)
        or "\\" in decoded_path
    ):
        raise HelpResourceError("Help link is not relative.")

    if any(part == ".." for part in decoded_path.split("/")):
        package_path = posixpath.normpath(posixpath.join(posixpath.dirname(current.package_path), decoded_path))
        resource = _RESOURCE_BY_PACKAGE_PATH.get(package_path)
        if resource is None:
            raise HelpResourceError("Help link leaves the approved document set.")
        return ResolvedHelpLink(resource, unquote(parsed.fragment))
    target_parts = _safe_relative_parts(decoded_path)
    parent_parts = PurePosixPath(current.package_path).parent.parts
    package_path = PurePosixPath(*parent_parts, *target_parts).as_posix()
    resource = _RESOURCE_BY_PACKAGE_PATH.get(package_path)
    if resource is None:
        return None
    return ResolvedHelpLink(resource, unquote(parsed.fragment))
