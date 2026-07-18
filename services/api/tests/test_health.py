from fastapi.testclient import TestClient

from app.main import app


def test_health_returns_ready_status() -> None:
    response = TestClient(app).get("/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_openapi_exposes_versioned_health_contract() -> None:
    schema = app.openapi()

    assert schema["info"]["version"] == "0.1.0"
    assert "/v1/health" in schema["paths"]
