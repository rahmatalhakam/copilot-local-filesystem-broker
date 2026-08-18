from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.config import ConfigurationError, McpConfig, load_config
from app.models import (
    FileOperationRequest,
    FileOperationResponse,
    Operation,
    Status,
)


EXPECTED_REQUEST_FIELDS = {
    "operation",
    "workspace",
    "path",
    "destinationPath",
    "content",
    "encoding",
    "overwrite",
    "appendNewLine",
    "createParentDirectories",
    "recursive",
    "force",
    "searchPattern",
    "searchText",
    "replacementText",
    "expectedOccurrences",
    "replaceAll",
    "caseSensitive",
    "useRegex",
    "wholeWord",
    "includeFiles",
    "includeDirectories",
    "includeHidden",
    "fileExtension",
    "namePattern",
    "maxDepth",
    "maxResults",
    "skip",
    "maxContentCharacters",
    "returnContent",
    "returnMetadata",
    "returnHash",
    "expectedHash",
    "expectedLastModifiedUtc",
    "shellCommand",
    "shellArguments",
    "reason",
    "timeoutSeconds",
    "correlationId",
}


def test_request_model_declares_complete_contract_and_defaults() -> None:
    request = FileOperationRequest(
        operation=Operation.READ_FILE,
        workspace="demo",
    )

    assert set(FileOperationRequest.model_fields) == EXPECTED_REQUEST_FIELDS
    assert request.encoding == "utf-8"
    assert request.shellArguments == []
    assert request.maxDepth == 10
    assert request.maxResults == 100
    assert request.timeoutSeconds == 20


def test_request_model_rejects_unknown_properties() -> None:
    with pytest.raises(ValidationError) as exc_info:
        FileOperationRequest.model_validate(
            {
                "operation": "EXISTS",
                "workspace": "demo",
                "path": "example.txt",
                "script": "Remove-Item -Recurse C:\\",
            }
        )

    assert exc_info.value.errors()[0]["type"] == "extra_forbidden"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("maxDepth", 21),
        ("maxResults", 0),
        ("maxContentCharacters", 400_001),
        ("timeoutSeconds", 61),
        ("shellArguments", ["argument"] * 51),
    ],
)
def test_request_model_enforces_declared_bounds(
    field: str,
    value: object,
) -> None:
    payload = {
        "operation": "EXISTS",
        "workspace": "demo",
        field: value,
    }

    with pytest.raises(ValidationError):
        FileOperationRequest.model_validate(payload)


def test_response_collection_defaults_are_not_shared() -> None:
    first = FileOperationResponse(
        success=True,
        status=Status.COMPLETED,
        operation="EXISTS",
        operationId="first",
        message="done",
    )
    second = FileOperationResponse(
        success=True,
        status=Status.COMPLETED,
        operation="EXISTS",
        operationId="second",
        message="done",
    )

    first.items.append(
        {
            "name": "one.txt",
            "relativePath": "one.txt",
            "itemType": "FILE",
        }
    )

    assert len(first.items) == 1
    assert second.items == []
    assert second.matches == []


def test_load_config_builds_isolated_workspace(
    config_path: Path,
    tmp_path: Path,
) -> None:
    config = load_config(config_path)

    assert config.host == "127.0.0.1"
    assert config.port == 8000
    assert config.log_directory == (tmp_path / "logs").resolve()
    assert set(config.workspaces) == {"test"}
    workspace = config.workspaces["test"]
    assert workspace.root == (tmp_path / "workspace").resolve()
    assert workspace.recycle_root == (tmp_path / "recycle").resolve()
    assert workspace.policy["allowed_extensions"] == [".txt", ".md", ".json"]
    assert workspace.command_policy["maximum_arguments"] == 20
    assert config.mcp == McpConfig()


def test_default_mcp_allowed_hosts_cover_local_bind_forms() -> None:
    assert McpConfig().allowed_hosts == [
        "127.0.0.1",
        "127.0.0.1:*",
        "0.0.0.0",
        "0.0.0.0:*",
        "localhost",
        "localhost:*",
        "[::1]",
        "[::1]:*",
        "[::]",
        "[::]:*",
    ]


def test_load_config_accepts_mcp_settings(
    config_path: Path,
    valid_config_data: dict[str, object],
) -> None:
    valid_config_data["mcp"] = {
        "enabled": False,
        "endpoint_path": "/agent/mcp",
        "allowed_hosts": [
            "broker.example.test",
            "broker.example.test:*",
            "[::1]",
            "[::1]:*",
        ],
        "allowed_origins": [
            "https://copilotstudio.microsoft.com",
            "http://[::1]:*",
        ],
    }
    config_path.write_text(
        yaml.safe_dump(valid_config_data, sort_keys=False),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.mcp.enabled is False
    assert config.mcp.endpoint_path == "/agent/mcp"
    assert config.mcp.allowed_hosts == [
        "broker.example.test",
        "broker.example.test:*",
        "[::1]",
        "[::1]:*",
    ]
    assert config.mcp.allowed_origins == [
        "https://copilotstudio.microsoft.com",
        "http://[::1]:*",
    ]


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("endpoint_path", "mcp", r"endpoint_path.*start with '/'"),
        ("endpoint_path", "/", r"endpoint_path.*must not be '/'"),
        ("allowed_hosts", [], r"allowed_hosts.*at least one"),
        ("allowed_hosts", ["broker/example"], r"allowed_hosts.*host"),
        ("allowed_origins", ["not-a-url"], r"allowed_origins.*origin"),
    ],
)
def test_config_rejects_invalid_mcp_settings(
    tmp_path: Path,
    valid_config_data: dict[str, object],
    field: str,
    value: object,
    match: str,
) -> None:
    valid_config_data["mcp"] = {
        "enabled": True,
        "endpoint_path": "/mcp",
        "allowed_hosts": ["127.0.0.1", "127.0.0.1:*"],
        "allowed_origins": [],
    }
    valid_config_data["mcp"][field] = value
    path = tmp_path / "invalid.yaml"
    path.write_text(yaml.safe_dump(valid_config_data), encoding="utf-8")

    with pytest.raises(ConfigurationError, match=match):
        load_config(path)


def test_load_config_uses_environment_path_when_not_explicit(
    config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BROKER_CONFIG_PATH", str(config_path))

    config = load_config()

    assert set(config.workspaces) == {"test"}


def test_explicit_path_takes_precedence_over_environment(
    config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BROKER_CONFIG_PATH", "does-not-exist.yaml")

    config = load_config(config_path)

    assert set(config.workspaces) == {"test"}


def test_missing_required_config_field_has_actionable_location(
    tmp_path: Path,
    valid_config_data: dict[str, object],
) -> None:
    workspace = valid_config_data["workspaces"]["test"]
    del workspace["policy"]["maximum_file_size_bytes"]
    path = tmp_path / "invalid.yaml"
    path.write_text(yaml.safe_dump(valid_config_data), encoding="utf-8")

    with pytest.raises(
        ConfigurationError,
        match=r"workspaces\.test\.policy\.maximum_file_size_bytes",
    ):
        load_config(path)


def test_config_rejects_non_boolean_permission(
    tmp_path: Path,
    valid_config_data: dict[str, object],
) -> None:
    valid_config_data["workspaces"]["test"]["permissions"]["read"] = "yes"
    path = tmp_path / "invalid.yaml"
    path.write_text(yaml.safe_dump(valid_config_data), encoding="utf-8")

    with pytest.raises(
        ConfigurationError,
        match=r"workspaces\.test\.permissions\.read.*boolean",
    ):
        load_config(path)


def test_config_rejects_write_limit_above_global_request_contract(
    tmp_path: Path,
    valid_config_data: dict[str, object],
) -> None:
    valid_config_data['workspaces']['test']['policy'][
        'maximum_write_characters'
    ] = 1_000_001
    path = tmp_path / 'invalid.yaml'
    path.write_text(yaml.safe_dump(valid_config_data), encoding='utf-8')

    with pytest.raises(
        ConfigurationError,
        match=r'maximum_write_characters.*1000000',
    ):
        load_config(path)


def test_config_rejects_relative_workspace_root(
    tmp_path: Path,
    valid_config_data: dict[str, object],
) -> None:
    valid_config_data["workspaces"]["test"]["root"] = "relative\\workspace"
    path = tmp_path / "invalid.yaml"
    path.write_text(yaml.safe_dump(valid_config_data), encoding="utf-8")

    with pytest.raises(
        ConfigurationError,
        match=r"workspaces\.test\.root.*absolute",
    ):
        load_config(path)


def test_config_rejects_overlapping_workspace_and_recycle_roots(
    tmp_path: Path,
    valid_config_data: dict[str, object],
) -> None:
    root = Path(valid_config_data["workspaces"]["test"]["root"])
    valid_config_data["workspaces"]["test"]["recycle_root"] = str(
        root / ".recycle"
    )
    path = tmp_path / "invalid.yaml"
    path.write_text(yaml.safe_dump(valid_config_data), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="must not overlap"):
        load_config(path)


def test_config_rejects_unapproved_command(
    tmp_path: Path,
    valid_config_data: dict[str, object],
) -> None:
    valid_config_data["workspaces"]["test"]["command_policy"][
        "allowed_commands"
    ] = ["Remove-Item"]
    path = tmp_path / "invalid.yaml"
    path.write_text(yaml.safe_dump(valid_config_data), encoding="utf-8")

    with pytest.raises(
        ConfigurationError,
        match=r"allowed_commands.*read-only",
    ):
        load_config(path)


def test_malformed_yaml_is_reported_as_configuration_error(
    tmp_path: Path,
) -> None:
    path = tmp_path / "invalid.yaml"
    path.write_text("server: [unterminated", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="YAML"):
        load_config(path)
