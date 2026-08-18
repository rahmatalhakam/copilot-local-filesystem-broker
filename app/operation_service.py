from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from typing import Any

from app.audit import append_audit_record
from app.config import AppConfig
from app.dispatcher import OperationValidationError, dispatch
from app.errors import PolicyViolation
from app.models import FileOperationRequest, FileOperationResponse, Status
from app.response_factory import make_error_response


_LOGGER = logging.getLogger("copilot_file_broker")
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


def _safe_string(value: object, maximum_length: int) -> str | None:
    if not isinstance(value, str):
        return None
    return value[:maximum_length]


def request_context(
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


def audit_record(
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


def write_audit_safely(
    app_config: AppConfig,
    context: Mapping[str, Any],
    response: FileOperationResponse,
) -> None:
    try:
        append_audit_record(
            app_config.log_directory,
            audit_record(context, response),
        )
    except Exception:
        failure = {
            "event": "audit_write_failed",
            "operationId": response.operationId,
            "correlationId": context["correlationId"],
        }
        _LOGGER.exception(json.dumps(failure, separators=(",", ":")))


def error_for_policy_violation(
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


def generic_error(
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


def default_request_timeout(
    app_config: AppConfig,
    request: FileOperationRequest,
) -> FileOperationRequest:
    if "timeoutSeconds" in request.model_fields_set:
        return request
    return request.model_copy(
        update={
            "timeoutSeconds": app_config.default_timeout_seconds,
        }
    )


def execute_operation(
    app_config: AppConfig,
    request: FileOperationRequest,
) -> tuple[int, FileOperationResponse]:
    started = time.perf_counter()
    request = default_request_timeout(app_config, request)
    context = request_context(request)

    try:
        response = dispatch(app_config, request)
        http_status = 200
    except PolicyViolation as error:
        duration = int((time.perf_counter() - started) * 1000)
        http_status, response = error_for_policy_violation(
            context,
            error,
            duration,
        )
    except FileNotFoundError:
        duration = int((time.perf_counter() - started) * 1000)
        http_status = 404
        response = generic_error(
            context,
            status=Status.NOT_FOUND,
            code="ITEM_NOT_FOUND",
            message="The requested file or directory was not found.",
            duration_ms=duration,
        )
    except FileExistsError:
        duration = int((time.perf_counter() - started) * 1000)
        http_status = 409
        response = generic_error(
            context,
            status=Status.CONFLICT,
            code="ITEM_ALREADY_EXISTS",
            message="The destination already exists.",
            duration_ms=duration,
        )
    except OperationValidationError as error:
        duration = int((time.perf_counter() - started) * 1000)
        http_status = 400
        response = generic_error(
            context,
            status=Status.FAILED,
            code="INVALID_OPERATION_FIELDS",
            message=str(error),
            duration_ms=duration,
        )
    except (IsADirectoryError, NotADirectoryError):
        duration = int((time.perf_counter() - started) * 1000)
        http_status = 400
        response = generic_error(
            context,
            status=Status.FAILED,
            code="INVALID_TARGET_TYPE",
            message="The target item type is invalid for this operation.",
            duration_ms=duration,
        )
    except UnicodeError:
        duration = int((time.perf_counter() - started) * 1000)
        http_status = 400
        response = generic_error(
            context,
            status=Status.FAILED,
            code="CONTENT_ENCODING_ERROR",
            message="File content is not valid for the selected encoding.",
            duration_ms=duration,
        )
    except TimeoutError:
        duration = int((time.perf_counter() - started) * 1000)
        http_status = 408
        response = generic_error(
            context,
            status=Status.TIMEOUT,
            code="EXECUTION_TIMEOUT",
            message="The operation exceeded its time limit.",
            duration_ms=duration,
        )
    except PermissionError:
        duration = int((time.perf_counter() - started) * 1000)
        http_status = 403
        response = generic_error(
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
        response = generic_error(
            context,
            status=Status.FAILED,
            code="INTERNAL_ERROR",
            message=(
                "The operation failed because of an internal server error."
            ),
            duration_ms=duration,
        )

    write_audit_safely(app_config, context, response)
    return http_status, response


__all__ = [
    "audit_record",
    "default_request_timeout",
    "error_for_policy_violation",
    "execute_operation",
    "generic_error",
    "request_context",
    "write_audit_safely",
]
