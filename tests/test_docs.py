from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_fastapi_docs_are_enabled() -> None:
    client = TestClient(app)

    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_yaml_spec_is_served() -> None:
    client = TestClient(app)

    response = client.get("/swagger/api-definition.swagger.yaml")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/yaml")
    assert response.text.lstrip().startswith("swagger: '2.0'")


def test_swagger_ui_page_references_yaml_spec() -> None:
    client = TestClient(app)

    response = client.get("/swagger-ui")

    assert response.status_code == 200
    assert "/swagger/api-definition.swagger.yaml" in response.text
