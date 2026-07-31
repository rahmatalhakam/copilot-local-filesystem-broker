from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.config import AppConfig, Workspace


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    root = tmp_path / "workspace"
    recycle_root = tmp_path / "recycle"
    root.mkdir()
    recycle_root.mkdir()

    return Workspace(
        alias="test",
        root=root.resolve(),
        recycle_root=recycle_root.resolve(),
        permissions={
            "read": True,
            "create": True,
            "update": True,
            "delete": True,
            "move": True,
            "copy": True,
            "search": True,
            "execute_command": True,
        },
        policy={
            "allowed_extensions": [".txt", ".md", ".json"],
            "maximum_file_size_bytes": 1024 * 1024,
            "maximum_write_characters": 100_000,
            "maximum_search_results": 100,
            "maximum_search_depth": 10,
            "allow_hidden_items": False,
            "allow_reparse_points": False,
            "allow_workspace_root_operation": False,
        },
        command_policy={
            "allowed_commands": [
                "Get-ChildItem",
                "Get-Content",
                "Test-Path",
                "Get-Item",
                "Select-String",
            ],
            "maximum_arguments": 20,
            "allow_environment_variables": False,
            "allow_wildcards": False,
        },
    )


@pytest.fixture
def app_config(tmp_path: Path, workspace: Workspace) -> AppConfig:
    return AppConfig(
        host="127.0.0.1",
        port=8000,
        log_directory=tmp_path / "logs",
        default_timeout_seconds=20,
        maximum_timeout_seconds=60,
        maximum_stdout_characters=10_000,
        maximum_stderr_characters=2_000,
        workspaces={workspace.alias: workspace},
    )


@pytest.fixture
def valid_config_data(tmp_path: Path) -> dict[str, object]:
    return {
        "server": {
            "host": "127.0.0.1",
            "port": 8000,
            "log_directory": str(tmp_path / "logs"),
            "default_timeout_seconds": 20,
            "maximum_timeout_seconds": 60,
            "maximum_stdout_characters": 10_000,
            "maximum_stderr_characters": 2_000,
        },
        "workspaces": {
            "test": {
                "root": str(tmp_path / "workspace"),
                "recycle_root": str(tmp_path / "recycle"),
                "permissions": {
                    "read": True,
                    "create": True,
                    "update": True,
                    "delete": True,
                    "move": True,
                    "copy": True,
                    "search": True,
                    "execute_command": True,
                },
                "policy": {
                    "allowed_extensions": [".txt", ".md", ".json"],
                    "maximum_file_size_bytes": 1024 * 1024,
                    "maximum_write_characters": 100_000,
                    "maximum_search_results": 100,
                    "maximum_search_depth": 10,
                    "allow_hidden_items": False,
                    "allow_reparse_points": False,
                    "allow_workspace_root_operation": False,
                },
                "command_policy": {
                    "allowed_commands": [
                        "Get-ChildItem",
                        "Get-Content",
                        "Test-Path",
                        "Get-Item",
                        "Select-String",
                    ],
                    "maximum_arguments": 20,
                    "allow_environment_variables": False,
                    "allow_wildcards": False,
                },
            }
        },
    }


@pytest.fixture
def config_path(
    tmp_path: Path,
    valid_config_data: dict[str, object],
) -> Path:
    path = tmp_path / "workspaces.yaml"
    path.write_text(
        yaml.safe_dump(valid_config_data, sort_keys=False),
        encoding="utf-8",
    )
    return path
