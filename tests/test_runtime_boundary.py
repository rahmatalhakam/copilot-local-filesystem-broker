from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.__main__ as launcher
import app.main as main_module
import app.operation_service as operation_service
from app.config import AppConfig
from app.main import create_app
from app.models import FileOperationResponse, Status


EXECUTE_PATH = '/api/v1/filesystem/execute'


def _audit_records(config: AppConfig) -> list[dict[str, Any]]:
    paths = list(config.log_directory.glob('audit-*.jsonl'))
    assert len(paths) == 1
    return [
        json.loads(line)
        for line in paths[0].read_text(encoding='utf-8').splitlines()
    ]


def _assert_too_large(response: Any) -> None:
    assert response.status_code == 413
    body = response.json()
    assert set(body) == set(FileOperationResponse.model_fields)
    assert body['success'] is False
    assert body['status'] == 'REJECTED'
    assert body['errorCode'] == 'REQUEST_BODY_TOO_LARGE'
    assert body['policyAllowed'] is False
    assert body['policyRule'] == 'request-body-size'
    assert response.headers['Cache-Control'] == 'no-store'


def test_content_length_over_limit_is_rejected_without_reading_body(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, 'REQUEST_BODY_LIMIT_BYTES', 64)
    client = TestClient(create_app(app_config))

    response = client.post(
        EXECUTE_PATH,
        content=b'{}',
        headers={
            'Content-Type': 'application/json',
            'Content-Length': '65',
        },
    )

    _assert_too_large(response)
    records = _audit_records(app_config)
    assert len(records) == 1
    assert records[0]['operation'] == 'UNKNOWN'
    assert records[0]['correlationId'] is None
    assert records[0]['errorCode'] == 'REQUEST_BODY_TOO_LARGE'
    assert 'content' not in records[0]


def test_chunked_body_over_limit_is_rejected_before_json_parsing(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, 'REQUEST_BODY_LIMIT_BYTES', 64)
    client = TestClient(create_app(app_config))

    def chunks():
        yield b'{' + (b'x' * 39)
        yield b'x' * 40

    response = client.post(
        EXECUTE_PATH,
        content=chunks(),
        headers={'Content-Type': 'application/json'},
    )

    assert 'content-length' not in response.request.headers
    _assert_too_large(response)
    assert len(_audit_records(app_config)) == 1


def test_under_limit_request_is_replayed_to_fastapi_normally(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, 'REQUEST_BODY_LIMIT_BYTES', 512)
    client = TestClient(create_app(app_config))

    response = client.post(
        EXECUTE_PATH,
        json={
            'operation': 'EXISTS',
            'workspace': 'test',
            'path': 'small.txt',
        },
    )

    assert response.status_code == 200
    assert response.json()['status'] == 'COMPLETED'


def test_configured_timeout_default_applies_only_when_omitted(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = replace(app_config, default_timeout_seconds=7)
    seen_timeouts: list[int] = []

    def capture_timeout(
        _config: AppConfig,
        request: Any,
    ) -> FileOperationResponse:
        seen_timeouts.append(request.timeoutSeconds)
        return FileOperationResponse(
            success=True,
            status=Status.COMPLETED,
            operation=request.operation.value,
            operationId='timeout-test',
            workspace=request.workspace,
            message='Done.',
        )

    monkeypatch.setattr(operation_service, 'dispatch', capture_timeout)
    client = TestClient(create_app(configured))

    omitted = client.post(
        EXECUTE_PATH,
        json={'operation': 'EXISTS', 'workspace': 'test'},
    )
    explicit = client.post(
        EXECUTE_PATH,
        json={
            'operation': 'EXISTS',
            'workspace': 'test',
            'timeoutSeconds': 13,
        },
    )

    assert omitted.status_code == 200
    assert explicit.status_code == 200
    assert seen_timeouts == [7, 13]


@pytest.mark.parametrize('path', ['/docs', '/redoc', '/openapi.json'])
def test_fastapi_documentation_routes_are_enabled(
    app_config: AppConfig,
    path: str,
) -> None:
    response = TestClient(create_app(app_config)).get(path)

    assert response.status_code == 200


def test_module_launcher_uses_configured_host_and_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(
        launcher,
        'CONFIG',
        SimpleNamespace(host='192.0.2.10', port=43123),
    )
    monkeypatch.setattr(
        launcher.uvicorn,
        'run',
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    launcher.main()

    assert calls == [
        (
            (launcher.app,),
            {'host': '192.0.2.10', 'port': 43123},
        )
    ]


def test_powershell_launcher_delegates_to_python_module() -> None:
    script = Path('run.ps1').read_text(encoding='utf-8')

    assert '& $python -m app' in script
    assert '--host' not in script
    assert '--port' not in script
