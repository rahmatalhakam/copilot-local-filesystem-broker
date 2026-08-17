from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mcp import Client

from app.config import AppConfig, McpConfig
from app.main import create_app
from app.mcp_server import create_mcp_server
from app.models import FileOperationResponse


def _audit_records(config: AppConfig) -> list[dict[str, Any]]:
    paths = list(config.log_directory.glob('audit-*.jsonl'))
    assert len(paths) == 1
    return [
        json.loads(line)
        for line in paths[0].read_text(encoding='utf-8').splitlines()
    ]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_mcp_server_exposes_single_workspace_operation_tool(
    app_config: AppConfig,
) -> None:
    server = create_mcp_server(app_config)

    async with Client(server, raise_exceptions=True) as client:
        result = await client.list_tools()

    assert [tool.name for tool in result.tools] == [
        "execute_workspace_file_operation"
    ]
    tool = result.tools[0]
    assert "controlled local workspace" in (tool.description or "")
    assert set(tool.input_schema["properties"]) == {"request"}
    assert tool.output_schema["title"] == "FileOperationResponse"


@pytest.mark.anyio
async def test_mcp_tool_reuses_filesystem_operation_contract(
    app_config: AppConfig,
) -> None:
    server = create_mcp_server(app_config)

    async with Client(server, raise_exceptions=True) as client:
        created = await client.call_tool(
            "execute_workspace_file_operation",
            {
                "request": {
                    "operation": "CREATE_FILE",
                    "workspace": "test",
                    "path": "notes/hello.txt",
                    "content": "hello from mcp",
                    "createParentDirectories": True,
                    "correlationId": "mcp-create",
                }
            },
        )
        read = await client.call_tool(
            "execute_workspace_file_operation",
            {
                "request": {
                    "operation": "READ_FILE",
                    "workspace": "test",
                    "path": "notes/hello.txt",
                    "correlationId": "mcp-read",
                }
            },
        )

    assert created.is_error is False
    assert created.structured_content["status"] == "COMPLETED"
    assert created.structured_content["operation"] == "CREATE_FILE"
    assert read.is_error is False
    assert read.structured_content["content"] == "hello from mcp"
    assert set(read.structured_content) == set(FileOperationResponse.model_fields)

    records = _audit_records(app_config)
    assert [record["correlationId"] for record in records] == [
        "mcp-create",
        "mcp-read",
    ]
    assert [record["operation"] for record in records] == [
        "CREATE_FILE",
        "READ_FILE",
    ]


@pytest.mark.anyio
async def test_mcp_policy_violation_returns_structured_broker_error(
    app_config: AppConfig,
) -> None:
    server = create_mcp_server(app_config)

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "execute_workspace_file_operation",
            {
                "request": {
                    "operation": "READ_FILE",
                    "workspace": "test",
                    "path": "..\\outside.txt",
                    "correlationId": "mcp-denied",
                }
            },
        )

    assert result.is_error is False
    assert result.structured_content["success"] is False
    assert result.structured_content["status"] == "REJECTED"
    assert result.structured_content["errorCode"] == "PATH_TRAVERSAL_DENIED"
    assert result.structured_content["policyAllowed"] is False
    assert _audit_records(app_config)[0]["correlationId"] == "mcp-denied"


def test_mcp_mount_is_not_registered_when_disabled(
    app_config: AppConfig,
) -> None:
    configured = replace(app_config, mcp=replace(app_config.mcp, enabled=False))

    response = TestClient(create_app(configured)).post("/mcp")

    assert response.status_code == 404


def test_mcp_mount_rejects_disallowed_host(
    app_config: AppConfig,
) -> None:
    configured = replace(
        app_config,
        mcp=McpConfig(
            enabled=True,
            endpoint_path="/mcp",
            allowed_hosts=["127.0.0.1", "127.0.0.1:*"],
            allowed_origins=[],
        ),
    )
    with TestClient(
        create_app(configured),
        base_url="http://evil.test",
    ) as client:
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"},
                },
            },
            headers={"Accept": "application/json, text/event-stream"},
        )

    assert response.status_code == 421


def test_mcp_mount_accepts_localhost_host(
    app_config: AppConfig,
) -> None:
    with TestClient(
        create_app(app_config),
        base_url="http://127.0.0.1",
        follow_redirects=False,
    ) as client:
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"},
                },
            },
            headers={"Accept": "application/json, text/event-stream"},
        )

    assert response.status_code in {200, 202}
    assert response.status_code != 421
