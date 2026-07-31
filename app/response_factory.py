from __future__ import annotations

import uuid

from app.models import FileOperationResponse, Status


def make_error_response(
    *,
    operation: str,
    correlation_id: str | None,
    workspace: str | None,
    path: str | None,
    destination_path: str | None,
    status: Status,
    error_code: str,
    message: str,
    policy_allowed: bool,
    policy_rule: str | None = None,
    duration_ms: int = 0,
) -> FileOperationResponse:
    """Build the same fixed response shape for every API error."""

    return FileOperationResponse(
        success=False,
        status=status,
        operation=operation,
        operationId=str(uuid.uuid4()),
        correlationId=correlation_id,
        workspace=workspace,
        path=path,
        destinationPath=destination_path,
        message=message,
        errorCode=error_code,
        errorMessage=message,
        durationMs=duration_ms,
        policyAllowed=policy_allowed,
        policyRule=policy_rule,
    )
