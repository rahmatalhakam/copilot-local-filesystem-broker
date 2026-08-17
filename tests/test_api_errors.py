from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.operation_service as operation_service
from app.config import AppConfig
from app.errors import PolicyViolation
from app.main import create_app
from app.models import FileOperationResponse, Status


EXECUTE_PATH = "/api/v1/filesystem/execute"
FIXED_RESPONSE_FIELDS = {
    "success",
    "status",
    "operation",
    "operationId",
    "correlationId",
    "workspace",
    "path",
    "destinationPath",
    "message",
    "errorCode",
    "errorMessage",
    "exists",
    "itemType",
    "content",
    "encoding",
    "contentTruncated",
    "name",
    "extension",
    "sizeBytes",
    "createdUtc",
    "modifiedUtc",
    "hash",
    "affectedCount",
    "totalResults",
    "returnedResults",
    "hasMore",
    "nextSkip",
    "items",
    "matches",
    "exitCode",
    "stdout",
    "stderr",
    "outputTruncated",
    "recycleId",
    "recyclePath",
    "durationMs",
    "policyAllowed",
    "policyRule",
}
SENSITIVE_AUDIT_FIELDS = {
    "content",
    "shellArguments",
    "stdout",
    "stderr",
}


def _client(app_config: AppConfig) -> TestClient:
    return TestClient(create_app(app_config))


def _read_audit_records(app_config: AppConfig) -> list[dict[str, Any]]:
    audit_paths = list(app_config.log_directory.glob("audit-*.jsonl"))
    assert len(audit_paths) == 1
    return [
        json.loads(line)
        for line in audit_paths[0].read_text(encoding="utf-8").splitlines()
    ]


def _assert_fixed_response_shape(body: Mapping[str, object]) -> None:
    assert set(body) == FIXED_RESPONSE_FIELDS
    assert isinstance(body["operationId"], str)
    assert body["operationId"]
    assert body["items"] == []
    assert body["matches"] == []


def _assert_security_headers(response: Any) -> None:
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"


def _raising_dispatch(error: Exception):
    def raise_error(*_: object) -> FileOperationResponse:
        raise error

    return raise_error


def test_audit_write_failure_does_not_mask_operation_response(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_audit_write(
        _log_directory: Path,
        _record: Mapping[str, Any],
    ) -> None:
        raise OSError("audit volume unavailable")

    monkeypatch.setattr(
        operation_service,
        "append_audit_record",
        fail_audit_write,
    )
    client = _client(app_config)

    response = client.post(
        EXECUTE_PATH,
        json={
            "operation": "EXISTS",
            "workspace": "test",
            "path": "not-created.txt",
            "correlationId": "audit-write-failure",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["status"] == "COMPLETED"
    assert body["exists"] is False
    _assert_fixed_response_shape(body)


def test_unexpected_exception_returns_generic_500_without_internal_details(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    internal_detail = (
        r"PRIVATE_INTERNAL_TOKEN at C:\broker\filesystem.py:417"
    )
    monkeypatch.setattr(
        operation_service,
        "dispatch",
        _raising_dispatch(RuntimeError(internal_detail)),
    )
    client = _client(app_config)

    response = client.post(
        EXECUTE_PATH,
        json={
            "operation": "EXISTS",
            "workspace": "test",
            "path": "item.txt",
            "correlationId": "unexpected-500",
        },
    )

    assert response.status_code == 500
    body = response.json()
    assert body["success"] is False
    assert body["status"] == "FAILED"
    assert body["errorCode"] == "INTERNAL_ERROR"
    assert body["errorMessage"] == (
        "The operation failed because of an internal server error."
    )
    serialized_response = response.text.casefold()
    assert internal_detail.casefold() not in serialized_response
    assert "private_internal_token" not in serialized_response
    assert "runtimeerror" not in serialized_response
    assert "traceback" not in serialized_response
    _assert_fixed_response_shape(body)

    records = _read_audit_records(app_config)
    assert len(records) == 1
    assert records[0]["correlationId"] == "unexpected-500"
    assert internal_detail not in json.dumps(records[0])


def test_operation_specific_field_error_returns_400_and_is_audited_once(
    app_config: AppConfig,
) -> None:
    client = _client(app_config)

    response = client.post(
        EXECUTE_PATH,
        json={
            "operation": "READ_FILE",
            "workspace": "test",
            "path": "item.txt",
            "shellCommand": "Get-ChildItem",
            "correlationId": "operation-fields-400",
        },
    )

    assert response.status_code == 400
    body = response.json()
    assert body["status"] == "FAILED"
    assert body["errorCode"] == "INVALID_OPERATION_FIELDS"
    assert body["policyAllowed"] is True
    _assert_fixed_response_shape(body)

    records = _read_audit_records(app_config)
    assert len(records) == 1
    assert records[0]["correlationId"] == "operation-fields-400"
    assert records[0]["errorCode"] == "INVALID_OPERATION_FIELDS"


@pytest.mark.parametrize(
    (
        "error",
        "expected_http_status",
        "expected_status",
        "expected_error_code",
        "expected_policy_allowed",
    ),
    [
        pytest.param(
            PolicyViolation(
                "PATH_TRAVERSAL",
                "The requested path is outside the workspace.",
                "workspace-containment",
            ),
            403,
            "REJECTED",
            "PATH_TRAVERSAL",
            False,
            id="policy-403",
        ),
        pytest.param(
            FileNotFoundError("private missing path"),
            404,
            "NOT_FOUND",
            "ITEM_NOT_FOUND",
            True,
            id="missing-404",
        ),
        pytest.param(
            TimeoutError("private timeout implementation detail"),
            408,
            "TIMEOUT",
            "EXECUTION_TIMEOUT",
            True,
            id="timeout-408",
        ),
        pytest.param(
            FileExistsError("private conflicting path"),
            409,
            "CONFLICT",
            "ITEM_ALREADY_EXISTS",
            True,
            id="conflict-409",
        ),
    ],
)
def test_domain_errors_map_to_fixed_http_error_contract(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected_http_status: int,
    expected_status: str,
    expected_error_code: str,
    expected_policy_allowed: bool,
) -> None:
    monkeypatch.setattr(
        operation_service,
        "dispatch",
        _raising_dispatch(error),
    )
    client = _client(app_config)

    response = client.post(
        EXECUTE_PATH,
        json={
            "operation": "EXISTS",
            "workspace": "test",
            "path": "item.txt",
            "correlationId": f"contract-{expected_http_status}",
        },
    )

    assert response.status_code == expected_http_status
    body = response.json()
    assert body["success"] is False
    assert body["status"] == expected_status
    assert body["errorCode"] == expected_error_code
    assert body["errorMessage"] == body["message"]
    assert body["policyAllowed"] is expected_policy_allowed
    _assert_fixed_response_shape(body)
    _assert_security_headers(response)

    records = _read_audit_records(app_config)
    assert len(records) == 1
    record = records[0]
    assert record["event"] == "filesystem_operation"
    assert record["correlationId"] == f"contract-{expected_http_status}"
    assert record["success"] is False
    assert record["status"] == expected_status
    assert record["errorCode"] == expected_error_code
    assert SENSITIVE_AUDIT_FIELDS.isdisjoint(record)


def test_success_and_error_responses_have_no_store_security_headers(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(app_config)
    success = client.get("/health")

    monkeypatch.setattr(
        operation_service,
        "dispatch",
        _raising_dispatch(TimeoutError()),
    )
    error = client.post(
        EXECUTE_PATH,
        json={
            "operation": "EXISTS",
            "workspace": "test",
            "path": "item.txt",
        },
    )

    assert success.status_code == 200
    assert error.status_code == 408
    _assert_security_headers(success)
    _assert_security_headers(error)


def test_each_accepted_request_writes_one_sanitized_audit_event(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_content = "REQUEST_FILE_CONTENT_SECRET"
    response_content = "RESPONSE_FILE_CONTENT_SECRET"
    command_argument = "COMMAND_ARGUMENT_SECRET"
    stdout_content = "COMMAND_STDOUT_SECRET"
    stderr_content = "COMMAND_STDERR_SECRET"

    def dispatch_with_sensitive_results(
        _config: AppConfig,
        request: Any,
    ) -> FileOperationResponse:
        values: dict[str, Any] = {}
        if request.operation.value == "CREATE_FILE":
            values["content"] = response_content
        else:
            values["stdout"] = stdout_content
            values["stderr"] = stderr_content

        return FileOperationResponse(
            success=True,
            status=Status.COMPLETED,
            operation=request.operation.value,
            operationId=f"operation-{request.correlationId}",
            correlationId=request.correlationId,
            workspace=request.workspace,
            path=request.path,
            message="Operation completed.",
            **values,
        )

    monkeypatch.setattr(
        operation_service,
        "dispatch",
        dispatch_with_sensitive_results,
    )
    client = _client(app_config)

    create_response = client.post(
        EXECUTE_PATH,
        json={
            "operation": "CREATE_FILE",
            "workspace": "test",
            "path": "secret.txt",
            "content": request_content,
            "correlationId": "audit-create",
        },
    )
    command_response = client.post(
        EXECUTE_PATH,
        json={
            "operation": "EXECUTE_COMMAND",
            "workspace": "test",
            "shellCommand": "Get-Content",
            "shellArguments": [command_argument],
            "correlationId": "audit-command",
        },
    )

    assert create_response.status_code == 200
    assert command_response.status_code == 200
    records = _read_audit_records(app_config)
    assert len(records) == 2
    assert {record["correlationId"] for record in records} == {
        "audit-create",
        "audit-command",
    }
    assert all(record["event"] == "filesystem_operation" for record in records)
    assert all(SENSITIVE_AUDIT_FIELDS.isdisjoint(record) for record in records)

    serialized_records = json.dumps(records)
    for sensitive_value in (
        request_content,
        response_content,
        command_argument,
        stdout_content,
        stderr_content,
    ):
        assert sensitive_value not in serialized_records

    command_record = next(
        record
        for record in records
        if record["correlationId"] == "audit-command"
    )
    assert command_record["shellArgumentCount"] == 1
