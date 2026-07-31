from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.errors import ConfigurationError


DEFAULT_CONFIG_PATH = Path("config/workspaces.yaml")
CONFIG_PATH_ENVIRONMENT_VARIABLE = "BROKER_CONFIG_PATH"

_PERMISSION_KEYS = frozenset(
    {
        "read",
        "create",
        "update",
        "delete",
        "move",
        "copy",
        "search",
        "execute_command",
    }
)
_POLICY_KEYS = frozenset(
    {
        "allowed_extensions",
        "maximum_file_size_bytes",
        "maximum_write_characters",
        "maximum_search_results",
        "maximum_search_depth",
        "allow_hidden_items",
        "allow_reparse_points",
        "allow_workspace_root_operation",
    }
)
_COMMAND_POLICY_KEYS = frozenset(
    {
        "allowed_commands",
        "maximum_arguments",
        "allow_environment_variables",
        "allow_wildcards",
    }
)
_READ_ONLY_COMMANDS = {
    command.casefold(): command
    for command in (
        "Get-ChildItem",
        "Get-Content",
        "Test-Path",
        "Get-Item",
        "Select-String",
    )
}
_EXTENSION_PATTERN = re.compile(r"^\.[A-Za-z0-9][A-Za-z0-9_-]{0,15}$")


@dataclass(frozen=True)
class Workspace:
    alias: str
    root: Path
    recycle_root: Path
    permissions: dict[str, bool]
    policy: dict[str, Any]
    command_policy: dict[str, Any]


@dataclass(frozen=True)
class AppConfig:
    host: str
    port: int
    log_directory: Path
    default_timeout_seconds: int
    maximum_timeout_seconds: int
    maximum_stdout_characters: int
    maximum_stderr_characters: int
    workspaces: dict[str, Workspace]


def _error(location: str, message: str) -> ConfigurationError:
    return ConfigurationError(f"Invalid configuration at '{location}': {message}")


def _as_mapping(value: object, location: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _error(location, "expected a mapping.")
    if any(not isinstance(key, str) for key in value):
        raise _error(location, "all keys must be strings.")
    return value


def _reject_unknown_keys(
    value: Mapping[str, object],
    allowed: frozenset[str],
    location: str,
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise _error(
            f"{location}.{unknown[0]}",
            "unknown field; check for a misspelled configuration key.",
        )


def _required(
    value: Mapping[str, object],
    key: str,
    location: str,
) -> object:
    if key not in value:
        raise _error(f"{location}.{key}", "field is required.")
    return value[key]


def _as_string(
    value: object,
    location: str,
    *,
    maximum_length: int | None = None,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _error(location, "expected a non-empty string.")
    result = value.strip()
    if maximum_length is not None and len(result) > maximum_length:
        raise _error(
            location,
            f"must contain at most {maximum_length} characters.",
        )
    if "\x00" in result:
        raise _error(location, "must not contain a null character.")
    return result


def _as_integer(
    value: object,
    location: str,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _error(location, "expected an integer.")
    if value < minimum:
        raise _error(location, f"must be at least {minimum}.")
    if maximum is not None and value > maximum:
        raise _error(location, f"must be at most {maximum}.")
    return value


def _as_boolean(value: object, location: str) -> bool:
    if not isinstance(value, bool):
        raise _error(location, "expected a boolean.")
    return value


def _as_absolute_path(value: object, location: str) -> Path:
    raw = _as_string(value, location)
    path = Path(raw)
    if not path.is_absolute():
        raise _error(location, "expected an absolute local filesystem path.")
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _error(location, f"path could not be normalized ({exc}).") from exc


def _paths_overlap(first: Path, second: Path) -> bool:
    try:
        first.relative_to(second)
        return True
    except ValueError:
        pass
    try:
        second.relative_to(first)
        return True
    except ValueError:
        return False


def _parse_permissions(value: object, location: str) -> dict[str, bool]:
    mapping = _as_mapping(value, location)
    _reject_unknown_keys(mapping, _PERMISSION_KEYS, location)

    permissions: dict[str, bool] = {}
    for key in sorted(_PERMISSION_KEYS):
        permissions[key] = _as_boolean(
            _required(mapping, key, location),
            f"{location}.{key}",
        )
    return permissions


def _parse_extensions(value: object, location: str) -> list[str]:
    if not isinstance(value, list):
        raise _error(location, "expected a list of file extensions.")

    extensions: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        item_location = f"{location}[{index}]"
        extension = _as_string(item, item_location).casefold()
        if not _EXTENSION_PATTERN.fullmatch(extension):
            raise _error(
                item_location,
                "expected a simple extension such as '.txt'.",
            )
        if extension in seen:
            raise _error(item_location, "duplicate extension.")
        extensions.append(extension)
        seen.add(extension)
    return extensions


def _parse_policy(value: object, location: str) -> dict[str, Any]:
    mapping = _as_mapping(value, location)
    _reject_unknown_keys(mapping, _POLICY_KEYS, location)

    return {
        "allowed_extensions": _parse_extensions(
            _required(mapping, "allowed_extensions", location),
            f"{location}.allowed_extensions",
        ),
        "maximum_file_size_bytes": _as_integer(
            _required(mapping, "maximum_file_size_bytes", location),
            f"{location}.maximum_file_size_bytes",
            minimum=1,
        ),
        "maximum_write_characters": _as_integer(
            _required(mapping, "maximum_write_characters", location),
            f"{location}.maximum_write_characters",
            minimum=1,
        ),
        "maximum_search_results": _as_integer(
            _required(mapping, "maximum_search_results", location),
            f"{location}.maximum_search_results",
            minimum=1,
            maximum=1000,
        ),
        "maximum_search_depth": _as_integer(
            _required(mapping, "maximum_search_depth", location),
            f"{location}.maximum_search_depth",
            minimum=0,
            maximum=20,
        ),
        "allow_hidden_items": _as_boolean(
            _required(mapping, "allow_hidden_items", location),
            f"{location}.allow_hidden_items",
        ),
        "allow_reparse_points": _as_boolean(
            _required(mapping, "allow_reparse_points", location),
            f"{location}.allow_reparse_points",
        ),
        "allow_workspace_root_operation": _as_boolean(
            _required(mapping, "allow_workspace_root_operation", location),
            f"{location}.allow_workspace_root_operation",
        ),
    }


def _parse_policy_with_global_limits(
    value: object,
    location: str,
) -> dict[str, Any]:
    policy = _parse_policy(value, location)
    if policy['maximum_write_characters'] > 1_000_000:
        raise _error(
            f'{location}.maximum_write_characters',
            'must be at most 1000000.',
        )
    return policy


def _parse_allowed_commands(value: object, location: str) -> list[str]:
    if not isinstance(value, list):
        raise _error(location, "expected a list of command names.")

    commands: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        item_location = f"{location}[{index}]"
        command = _as_string(item, item_location, maximum_length=100)
        folded = command.casefold()
        if folded not in _READ_ONLY_COMMANDS:
            raise _error(
                location,
                "only the approved read-only PowerShell commands are allowed.",
            )
        if folded in seen:
            raise _error(item_location, "duplicate command.")
        commands.append(_READ_ONLY_COMMANDS[folded])
        seen.add(folded)
    return commands


def _parse_command_policy(value: object, location: str) -> dict[str, Any]:
    mapping = _as_mapping(value, location)
    _reject_unknown_keys(mapping, _COMMAND_POLICY_KEYS, location)

    return {
        "allowed_commands": _parse_allowed_commands(
            _required(mapping, "allowed_commands", location),
            f"{location}.allowed_commands",
        ),
        "maximum_arguments": _as_integer(
            _required(mapping, "maximum_arguments", location),
            f"{location}.maximum_arguments",
            minimum=0,
            maximum=50,
        ),
        "allow_environment_variables": _as_boolean(
            _required(mapping, "allow_environment_variables", location),
            f"{location}.allow_environment_variables",
        ),
        "allow_wildcards": _as_boolean(
            _required(mapping, "allow_wildcards", location),
            f"{location}.allow_wildcards",
        ),
    }


def _parse_workspace(alias: str, value: object) -> Workspace:
    location = f"workspaces.{alias}"
    if not alias.strip() or len(alias) > 100:
        raise _error(location, "alias must contain between 1 and 100 characters.")
    if alias != alias.strip():
        raise _error(location, "alias must not start or end with whitespace.")

    mapping = _as_mapping(value, location)
    allowed_keys = frozenset(
        {"root", "recycle_root", "permissions", "policy", "command_policy"}
    )
    _reject_unknown_keys(mapping, allowed_keys, location)

    root = _as_absolute_path(
        _required(mapping, "root", location),
        f"{location}.root",
    )
    recycle_root = _as_absolute_path(
        _required(mapping, "recycle_root", location),
        f"{location}.recycle_root",
    )
    if _paths_overlap(root, recycle_root):
        raise _error(
            location,
            "workspace root and recycle root must not overlap.",
        )

    return Workspace(
        alias=alias,
        root=root,
        recycle_root=recycle_root,
        permissions=_parse_permissions(
            _required(mapping, "permissions", location),
            f"{location}.permissions",
        ),
        policy=_parse_policy_with_global_limits(
            _required(mapping, "policy", location),
            f"{location}.policy",
        ),
        command_policy=_parse_command_policy(
            _required(mapping, "command_policy", location),
            f"{location}.command_policy",
        ),
    )


def _parse_server(value: object) -> dict[str, object]:
    location = "server"
    mapping = _as_mapping(value, location)
    allowed_keys = frozenset(
        {
            "host",
            "port",
            "log_directory",
            "default_timeout_seconds",
            "maximum_timeout_seconds",
            "maximum_stdout_characters",
            "maximum_stderr_characters",
        }
    )
    _reject_unknown_keys(mapping, allowed_keys, location)

    maximum_timeout = _as_integer(
        _required(mapping, "maximum_timeout_seconds", location),
        "server.maximum_timeout_seconds",
        minimum=1,
        maximum=60,
    )
    default_timeout = _as_integer(
        _required(mapping, "default_timeout_seconds", location),
        "server.default_timeout_seconds",
        minimum=1,
        maximum=maximum_timeout,
    )

    return {
        "host": _as_string(
            _required(mapping, "host", location),
            "server.host",
            maximum_length=255,
        ),
        "port": _as_integer(
            _required(mapping, "port", location),
            "server.port",
            minimum=1,
            maximum=65535,
        ),
        "log_directory": _as_absolute_path(
            _required(mapping, "log_directory", location),
            "server.log_directory",
        ),
        "default_timeout_seconds": default_timeout,
        "maximum_timeout_seconds": maximum_timeout,
        "maximum_stdout_characters": _as_integer(
            _required(mapping, "maximum_stdout_characters", location),
            "server.maximum_stdout_characters",
            minimum=0,
        ),
        "maximum_stderr_characters": _as_integer(
            _required(mapping, "maximum_stderr_characters", location),
            "server.maximum_stderr_characters",
            minimum=0,
        ),
    }


def _validate_isolated_paths(
    server: Mapping[str, object],
    workspaces: Mapping[str, Workspace],
) -> None:
    locations: list[tuple[str, Path]] = [
        ("server.log_directory", server["log_directory"])
    ]
    for alias, workspace in workspaces.items():
        locations.extend(
            [
                (f"workspaces.{alias}.root", workspace.root),
                (f"workspaces.{alias}.recycle_root", workspace.recycle_root),
            ]
        )

    for index, (first_name, first_path) in enumerate(locations):
        for second_name, second_path in locations[index + 1 :]:
            if _paths_overlap(first_path, second_path):
                raise _error(
                    second_name,
                    f"path must not overlap '{first_name}'.",
                )


def _selected_config_path(path: Path | str | None) -> Path:
    if path is not None:
        return Path(path)

    configured_path = os.environ.get(CONFIG_PATH_ENVIRONMENT_VARIABLE)
    if configured_path is None:
        return DEFAULT_CONFIG_PATH
    if not configured_path.strip():
        raise ConfigurationError(
            f"{CONFIG_PATH_ENVIRONMENT_VARIABLE} must not be empty."
        )
    return Path(configured_path)


def load_config(path: Path | str | None = None) -> AppConfig:
    config_path = _selected_config_path(path)
    try:
        text = config_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ConfigurationError(
            f"Unable to read configuration file '{config_path}': {exc}"
        ) from exc

    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigurationError(
            f"Invalid YAML in configuration file '{config_path}': {exc}"
        ) from exc

    root = _as_mapping(loaded, "configuration")
    _reject_unknown_keys(root, frozenset({"server", "workspaces"}), "configuration")
    server = _parse_server(_required(root, "server", "configuration"))

    raw_workspaces = _as_mapping(
        _required(root, "workspaces", "configuration"),
        "workspaces",
    )
    if not raw_workspaces:
        raise _error("workspaces", "at least one workspace is required.")

    workspaces = {
        alias: _parse_workspace(alias, value)
        for alias, value in raw_workspaces.items()
    }
    _validate_isolated_paths(server, workspaces)

    return AppConfig(
        host=server["host"],
        port=server["port"],
        log_directory=server["log_directory"],
        default_timeout_seconds=server["default_timeout_seconds"],
        maximum_timeout_seconds=server["maximum_timeout_seconds"],
        maximum_stdout_characters=server["maximum_stdout_characters"],
        maximum_stderr_characters=server["maximum_stderr_characters"],
        workspaces=workspaces,
    )


__all__ = [
    "AppConfig",
    "ConfigurationError",
    "Workspace",
    "load_config",
]
