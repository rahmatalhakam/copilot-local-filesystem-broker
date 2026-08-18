from __future__ import annotations

import json
from collections.abc import AsyncIterator
from collections.abc import Mapping
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError as FastAPIValidationError
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import AppConfig, load_config
from app.mcp_server import create_mcp_asgi_app, create_mcp_server
from app.models import FileOperationRequest, FileOperationResponse, Status
from app.operation_service import (
    execute_operation,
    generic_error as _generic_error,
    request_context as _request_context,
    write_audit_safely as _write_audit_safely,
)


_EXECUTE_PATH = "/api/v1/filesystem/execute"
_SWAGGER_SPEC_PATH = Path(__file__).resolve().parents[1] / "swagger" / "api-definition.swagger.yaml"


REQUEST_BODY_LIMIT_BYTES = 16 * 1024 * 1024


def _json_response(
    response: FileOperationResponse,
    http_status: int,
) -> JSONResponse:
    result = JSONResponse(
        status_code=http_status,
        content=response.model_dump(mode="json"),
    )
    result.headers['Cache-Control'] = 'no-store'
    result.headers['X-Content-Type-Options'] = 'nosniff'
    result.headers['X-Frame-Options'] = 'DENY'
    return result


class RequestBodyLimitMiddleware:
    '''Bound request bytes before FastAPI parses or validates JSON.'''

    def __init__(
        self,
        app: ASGIApp,
        *,
        app_config: AppConfig,
        maximum_body_bytes: int,
    ) -> None:
        self.app = app
        self.app_config = app_config
        self.maximum_body_bytes = maximum_body_bytes

    async def _reject(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        context = _request_context({})
        response = _generic_error(
            context,
            status=Status.REJECTED,
            code='REQUEST_BODY_TOO_LARGE',
            message='The request body exceeds the service size limit.',
            duration_ms=0,
            policy_allowed=False,
            policy_rule='request-body-size',
        )
        _write_audit_safely(self.app_config, context, response)
        await _json_response(response, 413)(scope, receive, send)

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if (
            scope['type'] != 'http'
            or scope.get('method') != 'POST'
            or scope.get('path') != _EXECUTE_PATH
        ):
            await self.app(scope, receive, send)
            return

        for name, value in scope.get('headers', []):
            if name.lower() != b'content-length':
                continue
            try:
                declared_length = int(value)
            except ValueError:
                break
            if declared_length > self.maximum_body_bytes:
                await self._reject(scope, receive, send)
                return
            break

        buffered_messages: list[Message] = []
        received_bytes = 0
        while True:
            message = await receive()
            buffered_messages.append(message)
            if message['type'] != 'http.request':
                break

            received_bytes += len(message.get('body', b''))
            if received_bytes > self.maximum_body_bytes:
                await self._reject(scope, receive, send)
                return
            if not message.get('more_body', False):
                break

        message_index = 0

        async def replay_receive() -> Message:
            nonlocal message_index
            if message_index < len(buffered_messages):
                message = buffered_messages[message_index]
                message_index += 1
                return message
            return {'type': 'http.request', 'body': b'', 'more_body': False}

        await self.app(scope, replay_receive, send)


def create_app(app_config: AppConfig) -> FastAPI:
    mcp_server = None
    mcp_asgi_app = None
    lifespan = None
    if app_config.mcp.enabled:
        mcp_server = create_mcp_server(app_config)
        mcp_asgi_app = create_mcp_asgi_app(
            mcp_server,
            app_config,
            maximum_body_bytes=REQUEST_BODY_LIMIT_BYTES,
        )

        @asynccontextmanager
        async def mcp_lifespan(_: FastAPI) -> AsyncIterator[None]:
            async with mcp_server.session_manager.run():
                yield

        lifespan = mcp_lifespan

    application = FastAPI(
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        title="Copilot Local Filesystem Broker",
        version="1.0.0",
        description=(
            "Executes approved filesystem operations inside configured "
            "Windows workspaces."
        ),
        lifespan=lifespan,
    )
    application.state.config = app_config
    application.state.mcp_server = mcp_server
    application.add_middleware(
        RequestBodyLimitMiddleware,
        app_config=app_config,
        maximum_body_bytes=REQUEST_BODY_LIMIT_BYTES,
    )

    @application.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        return response

    @application.exception_handler(FastAPIValidationError)
    async def request_validation_error(
        request: Request,
        _: FastAPIValidationError,
    ) -> JSONResponse:
        raw: Mapping[str, object] = {}
        if request.url.path == _EXECUTE_PATH:
            try:
                value = await request.json()
                if isinstance(value, Mapping):
                    raw = value
            except (json.JSONDecodeError, UnicodeError):
                pass

        context = _request_context(raw)
        response = _generic_error(
            context,
            status=Status.FAILED,
            code="REQUEST_VALIDATION_ERROR",
            message="The request body does not match the declared schema.",
            duration_ms=0,
        )
        if request.url.path == _EXECUTE_PATH:
            _write_audit_safely(app_config, context, response)
        return _json_response(response, 422)

    @application.get("/health")
    def health() -> dict[str, str]:
        return {
            "status": "healthy",
            "service": "copilot-local-filesystem-broker",
        }

    @application.get("/swagger/api-definition.swagger.yaml")
    def swagger_spec() -> FileResponse:
        return FileResponse(
            _SWAGGER_SPEC_PATH,
            media_type="application/yaml",
            filename="api-definition.swagger.yaml",
        )

    @application.get("/swagger-ui")
    def swagger_ui() -> HTMLResponse:
        html = """
<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Copilot Local Filesystem Broker Swagger UI</title>
    <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css" />
    <style>
      html, body {
        margin: 0;
        padding: 0;
        height: 100%;
        background: #f7f7f7;
      }
      #swagger-ui {
        height: 100%;
      }
    </style>
  </head>
  <body>
    <div id="swagger-ui"></div>
    <script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
    <script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-standalone-preset.js"></script>
    <script>
      window.onload = () => {
        window.ui = SwaggerUIBundle({
          url: "/swagger/api-definition.swagger.yaml",
          dom_id: "#swagger-ui",
          deepLinking: true,
          presets: [
            SwaggerUIBundle.presets.apis,
            SwaggerUIStandalonePreset
          ],
          layout: "BaseLayout"
        });
      };
    </script>
  </body>
</html>
        """
        return HTMLResponse(html)

    @application.post(
        _EXECUTE_PATH,
        response_model=FileOperationResponse,
    )
    def execute(request: FileOperationRequest):
        http_status, response = execute_operation(app_config, request)
        if http_status == 200:
            return response
        return _json_response(response, http_status)

    if mcp_asgi_app is not None:
        application.mount("/", mcp_asgi_app)

    return application


CONFIG = load_config()
app = create_app(CONFIG)


__all__ = ["CONFIG", "app", "create_app"]
