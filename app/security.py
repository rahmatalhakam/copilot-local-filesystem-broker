from __future__ import annotations

import errno
import os
import stat
from pathlib import Path, PureWindowsPath

from app.config import Workspace
from app.errors import PolicyViolation


_INVALID_WINDOWS_FILENAME_CHARACTERS = frozenset('<>"|?*')
_RESERVED_WINDOWS_DEVICE_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "CLOCK$",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }
)


def is_reparse_point(path: Path) -> bool:
    """Return whether an existing path is a symlink or Windows reparse point."""

    try:
        path_stat = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        # An inaccessible path is not treated as safe by the resolver: its
        # later normalization/existence operation will fail closed.
        return False

    if stat.S_ISLNK(path_stat.st_mode):
        return True

    attributes = getattr(path_stat, "st_file_attributes", 0)
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_attribute)


def is_hidden(path: Path) -> bool:
    """Return whether a path is dot-hidden or has the Windows hidden flag."""

    if path.name.startswith(".") and path.name not in {".", ".."}:
        return True

    try:
        path_stat = os.stat(path, follow_symlinks=False)
    except (FileNotFoundError, NotADirectoryError, OSError):
        return False

    attributes = getattr(path_stat, "st_file_attributes", 0)
    hidden_attribute = getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0x2)
    return bool(attributes & hidden_attribute)


def _raise_path_syntax(message: str) -> None:
    raise PolicyViolation(
        "WINDOWS_PATH_SYNTAX_DENIED",
        message,
        "deny-ambiguous-windows-path",
    )


def _validate_windows_components(parts: tuple[str, ...]) -> None:
    for part in parts:
        if len(part) > 255:
            _raise_path_syntax(
                "Windows path components may not exceed 255 characters."
            )
        if part.endswith((" ", ".")):
            _raise_path_syntax(
                "Path components ending in a space or period are not permitted."
            )
        if any(
            character in _INVALID_WINDOWS_FILENAME_CHARACTERS
            or ord(character) < 32
            for character in part
        ):
            _raise_path_syntax(
                "The path contains a character that is invalid on Windows."
            )

        device_name = part.split(".", maxsplit=1)[0].upper()
        if device_name in _RESERVED_WINDOWS_DEVICE_NAMES:
            raise PolicyViolation(
                "WINDOWS_DEVICE_PATH_DENIED",
                "Windows device names are not permitted as path components.",
                "deny-windows-device-path",
            )


def _enforce_ancestor_policy(
    workspace: Workspace,
    root: Path,
    parts: tuple[str, ...],
) -> None:
    allow_reparse_points = (
        workspace.policy.get("allow_reparse_points") is True
    )
    allow_hidden_items = workspace.policy.get("allow_hidden_items") is True

    current = root
    for part in (None, *parts):
        if part is not None:
            current = current / part

        if not allow_reparse_points and is_reparse_point(current):
            raise PolicyViolation(
                "REPARSE_POINT_DENIED",
                (
                    "Operations through symbolic links, junctions, or other "
                    "reparse points are not permitted."
                ),
                "deny-reparse-point",
            )
        if not allow_hidden_items and is_hidden(current):
            raise PolicyViolation(
                "HIDDEN_ITEM_DENIED",
                "Operations on hidden paths are not permitted.",
                "deny-hidden-item",
            )


def resolve_workspace_path(
    workspace: Workspace,
    relative_path: str | None,
    *,
    must_exist: bool = False,
    allow_root: bool = False,
) -> Path:
    """Resolve one literal Windows-relative path beneath a workspace root."""

    if relative_path is None or not relative_path.strip():
        raw = "."
    else:
        raw = relative_path

    if len(raw) > 1000:
        _raise_path_syntax("Relative paths may not exceed 1000 characters.")

    # Check UNC and device namespaces before the generic absolute-path check so
    # callers receive the stable, specific policy code.
    if raw.startswith(("\\\\", "//")):
        raise PolicyViolation(
            "UNC_PATH_DENIED",
            "UNC paths are not permitted.",
            "deny-unc-path",
        )

    win_path = PureWindowsPath(raw)
    if win_path.is_absolute() or win_path.drive or win_path.root:
        raise PolicyViolation(
            "ABSOLUTE_PATH_DENIED",
            "Absolute, rooted, or drive-qualified paths are not permitted.",
            "deny-absolute-path",
        )

    if any(part == ".." for part in win_path.parts):
        raise PolicyViolation(
            "PATH_TRAVERSAL_DENIED",
            "Parent-directory traversal is not permitted.",
            "deny-parent-traversal",
        )

    # The drive check above has already accounted for a normal drive colon.
    if ":" in raw:
        raise PolicyViolation(
            "ALTERNATE_DATA_STREAM_DENIED",
            "Colon characters are not permitted in relative paths.",
            "deny-alternate-data-stream",
        )

    parts = tuple(win_path.parts)
    _validate_windows_components(parts)

    configured_root = workspace.root
    if not configured_root.is_absolute():
        raise PolicyViolation(
            "WORKSPACE_CONFIGURATION_INVALID",
            "The configured workspace root must be absolute.",
            "valid-workspace-root",
        )

    # Inspect the lexical chain before resolve() follows any existing link.
    _enforce_ancestor_policy(workspace, configured_root, parts)

    try:
        root = configured_root.resolve(strict=False)
        candidate = (root / Path(*parts)).resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise PolicyViolation(
            "PATH_RESOLUTION_FAILED",
            "The path could not be resolved safely.",
            "canonical-workspace-path",
        ) from exc

    try:
        resolved_parts = candidate.relative_to(root).parts
    except ValueError as exc:
        raise PolicyViolation(
            "WORKSPACE_ESCAPE_DENIED",
            "The resolved path is outside the configured workspace.",
            "deny-workspace-escape",
        ) from exc

    if candidate == root and not allow_root:
        raise PolicyViolation(
            "WORKSPACE_ROOT_OPERATION_DENIED",
            "This operation is not allowed on the workspace root.",
            "deny-workspace-root-operation",
        )

    # Recheck immediately before returning. This narrows (but cannot eliminate)
    # the race between policy validation and an eventual filesystem operation.
    _enforce_ancestor_policy(workspace, root, resolved_parts)

    if must_exist and not candidate.exists():
        raise FileNotFoundError(
            errno.ENOENT,
            os.strerror(errno.ENOENT),
            str(candidate),
        )

    return candidate


def validate_extension(workspace: Workspace, path: Path) -> None:
    allowed = {
        extension.casefold()
        for extension in workspace.policy.get("allowed_extensions", [])
        if isinstance(extension, str)
    }
    extension = path.suffix.casefold()

    if extension not in allowed:
        raise PolicyViolation(
            "EXTENSION_DENIED",
            f"The extension '{path.suffix}' is not allowed.",
            "allowlisted-file-extensions",
        )


# Retain the private names used by the implementation sketch.
_is_hidden = is_hidden
_is_reparse_point = is_reparse_point

__all__ = [
    "PolicyViolation",
    "is_hidden",
    "is_reparse_point",
    "resolve_workspace_path",
    "validate_extension",
]
