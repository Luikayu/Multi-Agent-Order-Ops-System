from fastapi.testclient import TestClient

from order_agent_ops.main import app


def test_fastapi_application_can_be_imported() -> None:
    assert app.title == "order-agent-ops"
    assert app.version == "0.1.0"


def test_health_returns_application_metadata() -> None:
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "name": "order-agent-ops",
        "version": "0.1.0",
        "status": "ok",
    }
