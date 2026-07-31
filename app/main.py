from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError as FastAPIValidationError
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.audit import append_audit_record
from app.config import AppConfig, load_config
from app.dispatcher import OperationValidationError, dispatch
from app.errors import PolicyViolation
from app.models import FileOperationRequest, FileOperationResponse, Status
from app.response_factory import make_error_response


_LOGGER = logging.getLogger("copilot_file_broker")
_EXECUTE_PATH = "/api/v1/filesystem/execute"
_CONFLICT_CODES = {
    "HASH_MISMATCH",
    "LAST_MODIFIED_MISMATCH",
    "UNEXPECTED_MATCH_COUNT",
}
_BAD_REQUEST_POLICY_CODES = {
    "DESTINATION_INSIDE_SOURCE",
    "EMPTY_SEARCH_TEXT",
    "INCOMPATIBLE_TARGET_TYPE",
    "INVALID_BASE64",
    "INVALID_REGEX",
    "RECURSIVE_REQUIRED",
}


REQUEST_BODY_LIMIT_BYTES = 16 * 1024 * 1024


def _safe_string(value: object, maximum_length: int) -> str | None:
    if not isinstance(value, str):
        return None
    return value[:maximum_length]


def _request_context(
    request: FileOperationRequest | Mapping[str, object],
) -> dict[str, Any]:
    if isinstance(request, FileOperationRequest):
        return {
            "operation": request.operation.value,
            "correlationId": request.correlationId,
            "workspace": request.workspace,
            "path": request.path,
            "destinationPath": request.destinationPath,
            "shellCommand": request.shellCommand,
            "shellArgumentCount": len(request.shellArguments),
            "reason": request.reason,
        }

    arguments = request.get("shellArguments")
    return {
        "operation": _safe_string(request.get("operation"), 100) or "UNKNOWN",
        "correlationId": _safe_string(request.get("correlationId"), 200),
        "workspace": _safe_string(request.get("workspace"), 100),
        "path": _safe_string(request.get("path"), 1000),
        "destinationPath": _safe_string(
            request.get("destinationPath"),
            1000,
        ),
        "shellCommand": _safe_string(request.get("shellCommand"), 100),
        "shellArgumentCount": (
            min(len(arguments), 50) if isinstance(arguments, list) else 0
        ),
        "reason": _safe_string(request.get("reason"), 1000),
    }


def _audit_record(
    context: Mapping[str, Any],
    response: FileOperationResponse,
) -> dict[str, Any]:
    return {
        "event": "filesystem_operation",
        "operationId": response.operationId,
        "correlationId": context["correlationId"],
        "workspace": context["workspace"],
        "operation": context["operation"],
        "path": context["path"],
        "destinationPath": context["destinationPath"],
        "shellCommand": context["shellCommand"],
        "shellArgumentCount": context["shellArgumentCount"],
        "reason": context["reason"],
        "success": response.success,
        "status": response.status.value,
        "errorCode": response.errorCode,
        "durationMs": response.durationMs,
        "policyAllowed": response.policyAllowed,
        "policyRule": response.policyRule,
        "outputTruncated": response.outputTruncated,
    }


def _write_audit_safely(
    app_config: AppConfig,
    context: Mapping[str, Any],
    response: FileOperationResponse,
) -> None:
    try:
        append_audit_record(
            app_config.log_directory,
            _audit_record(context, response),
        )
    except Exception:
        failure = {
            "event": "audit_write_failed",
            "operationId": response.operationId,
            "correlationId": context["correlationId"],
        }
        _LOGGER.exception(json.dumps(failure, separators=(",", ":")))


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


def _error_for_policy_violation(
    context: Mapping[str, Any],
    error: PolicyViolation,
    duration_ms: int,
) -> tuple[int, FileOperationResponse]:
    if error.code in _CONFLICT_CODES:
        http_status = 409
        status = Status.CONFLICT
        policy_allowed = True
    elif error.code in _BAD_REQUEST_POLICY_CODES:
        http_status = 400
        status = Status.FAILED
        policy_allowed = True
    else:
        http_status = 403
        status = Status.REJECTED
        policy_allowed = False

    return http_status, make_error_response(
        operation=context["operation"],
        correlation_id=context["correlationId"],
        workspace=context["workspace"],
        path=context["path"],
        destination_path=context["destinationPath"],
        status=status,
        error_code=error.code,
        message=error.message,
        policy_allowed=policy_allowed,
        policy_rule=error.rule,
        duration_ms=duration_ms,
    )


def _generic_error(
    context: Mapping[str, Any],
    *,
    status: Status,
    code: str,
    message: str,
    duration_ms: int,
    policy_allowed: bool = True,
    policy_rule: str | None = None,
) -> FileOperationResponse:
    return make_error_response(
        operation=context["operation"],
        correlation_id=context["correlationId"],
        workspace=context["workspace"],
        path=context["path"],
        destination_path=context["destinationPath"],
        status=status,
        error_code=code,
        message=message,
        policy_allowed=policy_allowed,
        policy_rule=policy_rule,
        duration_ms=duration_ms,
    )


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
    application = FastAPI(
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        title="Copilot Local Filesystem Broker",
        version="1.0.0",
        description=(
            "Executes approved filesystem operations inside configured "
            "Windows workspaces."
        ),
    )
    application.state.config = app_config
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

    @application.post(
        _EXECUTE_PATH,
        response_model=FileOperationResponse,
    )
    def execute(request: FileOperationRequest):
        started = time.perf_counter()
        if 'timeoutSeconds' not in request.model_fields_set:
            request = request.model_copy(
                update={
                    'timeoutSeconds': app_config.default_timeout_seconds,
                }
            )
        context = _request_context(request)

        try:
            response = dispatch(app_config, request)
            http_status = 200
        except PolicyViolation as error:
            duration = int((time.perf_counter() - started) * 1000)
            http_status, response = _error_for_policy_violation(
                context,
                error,
                duration,
            )
        except FileNotFoundError:
            duration = int((time.perf_counter() - started) * 1000)
            http_status = 404
            response = _generic_error(
                context,
                status=Status.NOT_FOUND,
                code="ITEM_NOT_FOUND",
                message="The requested file or directory was not found.",
                duration_ms=duration,
            )
        except FileExistsError:
            duration = int((time.perf_counter() - started) * 1000)
            http_status = 409
            response = _generic_error(
                context,
                status=Status.CONFLICT,
                code="ITEM_ALREADY_EXISTS",
                message="The destination already exists.",
                duration_ms=duration,
            )
        except OperationValidationError as error:
            duration = int((time.perf_counter() - started) * 1000)
            http_status = 400
            response = _generic_error(
                context,
                status=Status.FAILED,
                code="INVALID_OPERATION_FIELDS",
                message=str(error),
                duration_ms=duration,
            )
        except (IsADirectoryError, NotADirectoryError):
            duration = int((time.perf_counter() - started) * 1000)
            http_status = 400
            response = _generic_error(
                context,
                status=Status.FAILED,
                code="INVALID_TARGET_TYPE",
                message="The target item type is invalid for this operation.",
                duration_ms=duration,
            )
        except UnicodeError:
            duration = int((time.perf_counter() - started) * 1000)
            http_status = 400
            response = _generic_error(
                context,
                status=Status.FAILED,
                code="CONTENT_ENCODING_ERROR",
                message="File content is not valid for the selected encoding.",
                duration_ms=duration,
            )
        except TimeoutError:
            duration = int((time.perf_counter() - started) * 1000)
            http_status = 408
            response = _generic_error(
                context,
                status=Status.TIMEOUT,
                code="EXECUTION_TIMEOUT",
                message="The operation exceeded its time limit.",
                duration_ms=duration,
            )
        except PermissionError:
            duration = int((time.perf_counter() - started) * 1000)
            http_status = 403
            response = _generic_error(
                context,
                status=Status.REJECTED,
                code="FILESYSTEM_ACCESS_DENIED",
                message="The process account cannot access the requested item.",
                duration_ms=duration,
                policy_allowed=False,
                policy_rule="windows-filesystem-acl",
            )
        except Exception:
            duration = int((time.perf_counter() - started) * 1000)
            failure = {
                "event": "filesystem_operation_failed",
                "correlationId": context["correlationId"],
                "operation": context["operation"],
            }
            _LOGGER.exception(json.dumps(failure, separators=(",", ":")))
            http_status = 500
            response = _generic_error(
                context,
                status=Status.FAILED,
                code="INTERNAL_ERROR",
                message=(
                    "The operation failed because of an internal server error."
                ),
                duration_ms=duration,
            )

        _write_audit_safely(app_config, context, response)
        if http_status == 200:
            return response
        return _json_response(response, http_status)

    return application


CONFIG = load_config()
app = create_app(CONFIG)


__all__ = ["CONFIG", "app", "create_app"]
