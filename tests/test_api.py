from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import AppConfig, Workspace
from app.main import create_app


def make_config(tmp_path: Path) -> AppConfig:
    workspace_root = tmp_path / "workspace"
    recycle_root = tmp_path / "recycle"
    workspace_root.mkdir()
    recycle_root.mkdir()

    workspace = Workspace(
        alias="test",
        root=workspace_root,
        recycle_root=recycle_root,
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
    return AppConfig(
        host="127.0.0.1",
        port=8000,
        log_directory=tmp_path / "logs",
        default_timeout_seconds=20,
        maximum_timeout_seconds=60,
        maximum_stdout_characters=10_000,
        maximum_stderr_characters=2_000,
        workspaces={"test": workspace},
    )


def client_for(tmp_path: Path) -> tuple[TestClient, AppConfig]:
    config = make_config(tmp_path)
    return TestClient(create_app(config)), config


def audit_records(config: AppConfig) -> list[dict[str, object]]:
    paths = list(config.log_directory.glob("audit-*.jsonl"))
    assert len(paths) == 1
    return [
        json.loads(line)
        for line in paths[0].read_text(encoding="utf-8").splitlines()
    ]


def test_health_reports_service_status(tmp_path: Path) -> None:
    client, _ = client_for(tmp_path)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "healthy",
        "service": "copilot-local-filesystem-broker",
    }


def test_create_and_read_file_through_single_endpoint(tmp_path: Path) -> None:
    client, _ = client_for(tmp_path)

    created = client.post(
        "/api/v1/filesystem/execute",
        json={
            "operation": "CREATE_FILE",
            "workspace": "test",
            "path": r"notes\hello.txt",
            "content": "Hello from the connector",
            "createParentDirectories": True,
            "returnHash": True,
            "correlationId": "create-1",
        },
    )
    read = client.post(
        "/api/v1/filesystem/execute",
        json={
            "operation": "READ_FILE",
            "workspace": "test",
            "path": r"notes\hello.txt",
            "returnHash": True,
            "correlationId": "read-1",
        },
    )

    assert created.status_code == 200
    assert created.json()["success"] is True
    assert created.json()["status"] == "COMPLETED"
    assert created.json()["hash"].startswith("sha256:")
    assert read.status_code == 200
    body = read.json()
    assert body["content"] == "Hello from the connector"
    assert body["hash"] == created.json()["hash"]
    assert body["items"] == []
    assert body["matches"] == []
    assert body["stdout"] == ""
    assert body["stderr"] == ""


def test_unknown_request_property_is_rejected_and_audited(tmp_path: Path) -> None:
    client, config = client_for(tmp_path)

    response = client.post(
        "/api/v1/filesystem/execute",
        json={
            "operation": "EXISTS",
            "workspace": "test",
            "path": "example.txt",
            "unexpected": "value",
            "correlationId": "invalid-1",
        },
    )

    assert response.status_code == 422
    assert response.json()["errorCode"] == "REQUEST_VALIDATION_ERROR"
    records = audit_records(config)
    assert len(records) == 1
    assert records[0]["status"] == "FAILED"
    assert records[0]["correlationId"] == "invalid-1"


def test_shell_fields_on_structured_operation_are_rejected(tmp_path: Path) -> None:
    client, _ = client_for(tmp_path)

    response = client.post(
        "/api/v1/filesystem/execute",
        json={
            "operation": "READ_FILE",
            "workspace": "test",
            "path": "example.txt",
            "shellCommand": "Get-ChildItem",
        },
    )

    assert response.status_code == 400
    assert response.json()["errorCode"] == "INVALID_OPERATION_FIELDS"


def test_policy_missing_and_conflict_errors_use_stable_shape(tmp_path: Path) -> None:
    client, _ = client_for(tmp_path)

    denied = client.post(
        "/api/v1/filesystem/execute",
        json={
            "operation": "READ_FILE",
            "workspace": "test",
            "path": r"..\outside.txt",
        },
    )
    missing = client.post(
        "/api/v1/filesystem/execute",
        json={
            "operation": "READ_FILE",
            "workspace": "test",
            "path": "missing.txt",
        },
    )
    first_create = client.post(
        "/api/v1/filesystem/execute",
        json={
            "operation": "CREATE_FILE",
            "workspace": "test",
            "path": "duplicate.txt",
            "content": "first",
        },
    )
    conflict = client.post(
        "/api/v1/filesystem/execute",
        json={
            "operation": "CREATE_FILE",
            "workspace": "test",
            "path": "duplicate.txt",
            "content": "second",
        },
    )

    assert denied.status_code == 403
    assert denied.json()["status"] == "REJECTED"
    assert denied.json()["policyAllowed"] is False
    assert missing.status_code == 404
    assert missing.json()["status"] == "NOT_FOUND"
    assert first_create.status_code == 200
    assert conflict.status_code == 409
    assert conflict.json()["status"] == "CONFLICT"
    assert set(conflict.json()) >= {
        "success",
        "status",
        "operation",
        "operationId",
        "message",
        "errorCode",
        "items",
        "matches",
    }


def test_audit_record_omits_content_and_operation_output(tmp_path: Path) -> None:
    client, config = client_for(tmp_path)

    response = client.post(
        "/api/v1/filesystem/execute",
        json={
            "operation": "CREATE_FILE",
            "workspace": "test",
            "path": "secret.txt",
            "content": "TOP_SECRET_CONTENT",
            "correlationId": "audit-1",
        },
    )

    assert response.status_code == 200
    records = audit_records(config)
    assert len(records) == 1
    serialized = json.dumps(records[0])
    assert "TOP_SECRET_CONTENT" not in serialized
    assert "content" not in records[0]
    assert "stdout" not in records[0]
    assert "stderr" not in records[0]
    assert records[0]["event"] == "filesystem_operation"
    assert records[0]["correlationId"] == "audit-1"


def test_unknown_workspace_is_a_policy_rejection(tmp_path: Path) -> None:
    client, _ = client_for(tmp_path)

    response = client.post(
        "/api/v1/filesystem/execute",
        json={
            "operation": "EXISTS",
            "workspace": "not-configured",
            "path": "example.txt",
        },
    )

    assert response.status_code == 403
    assert response.json()["errorCode"] == "WORKSPACE_NOT_FOUND"
