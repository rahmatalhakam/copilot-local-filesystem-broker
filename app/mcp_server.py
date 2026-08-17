from __future__ import annotations

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette

from app.config import AppConfig
from app.models import FileOperationRequest, FileOperationResponse
from app.operation_service import execute_operation


def create_mcp_server(app_config: AppConfig) -> MCPServer:
    server = MCPServer(
        name="copilot-local-filesystem-broker",
        title="Copilot Local Filesystem Broker",
        description=(
            "Execute controlled local workspace filesystem operations through "
            "configured workspace aliases."
        ),
        version="1.0.0",
    )

    @server.tool(
        name="execute_workspace_file_operation",
        title="Execute workspace file operation",
        description=(
            "Execute one controlled local workspace filesystem operation. "
            "Use configured workspace aliases and relative paths only. "
            "The response is always the broker's structured operation result."
        ),
    )
    def execute_workspace_file_operation(
        request: FileOperationRequest,
    ) -> FileOperationResponse:
        """Execute a controlled local workspace filesystem operation."""

        _, response = execute_operation(app_config, request)
        return response

    return server


def create_mcp_asgi_app(
    server: MCPServer,
    app_config: AppConfig,
    *,
    maximum_body_bytes: int,
) -> Starlette:
    security = TransportSecuritySettings(
        allowed_hosts=app_config.mcp.allowed_hosts,
        allowed_origins=app_config.mcp.allowed_origins,
    )
    return server.streamable_http_app(
        streamable_http_path=app_config.mcp.endpoint_path,
        max_request_body_size=maximum_body_bytes,
        transport_security=security,
        host=app_config.host,
    )


__all__ = ["create_mcp_asgi_app", "create_mcp_server"]
