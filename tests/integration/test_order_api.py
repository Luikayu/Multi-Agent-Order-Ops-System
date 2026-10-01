from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from order_agent_ops.domain.enums import OrderStatus
from order_agent_ops.main import create_app
from order_agent_ops.models import MockModelProvider


def make_app(tmp_path: Path, provider: MockModelProvider | None = None) -> FastAPI:
    return create_app(
        database_path=tmp_path / "api.sqlite3",
        provider=provider or MockModelProvider(),
    )


def order_payload(
    *,
    sku: str = "SKU-001",
    quantity: int = 1,
    user_id: str = "USER-001",
    key: str = "api-order-001",
) -> dict[str, object]:
    unit_price = "399.00" if sku == "SKU-001" else "159.00"
    total = str(float(unit_price) * quantity)
    return {
        "user_id": user_id,
        "items": [
            {"sku": sku, "quantity": quantity, "unit_price": unit_price}
        ],
        "total_amount": total,
        "idempotency_key": key,
    }


def test_happy_path_completes_and_can_be_queried_by_order_id(tmp_path: Path) -> None:
    application = make_app(tmp_path)
    with TestClient(application) as client:
        response = client.post("/orders", json=order_payload())

        assert response.status_code == 200
        submitted = response.json()
        assert submitted["status"] == "COMPLETED"
        assert submitted["trace_id"].startswith("TRACE-")
        assert submitted["idempotent_replay"] is False

        lookup = client.get(f"/orders/{submitted['order_id']}")
        assert lookup.status_code == 200
        assert lookup.json()["status"] == "COMPLETED"
        assert lookup.json()["trace_id"] == submitted["trace_id"]

        inventory = application.state.inventory_repository.get("SKU-001")
        assert inventory.quantity == 11
        spans = application.state.trace_recorder.get_trace(submitted["trace_id"])
        assert {
            (span.component, span.action)
            for span in spans
        }.issuperset(
            {
                ("order-workflow", "order.request"),
                ("order-coordinator-agent", "coordinator.plan_and_dispatch"),
                ("inventory-agent", "inventory.check"),
                ("risk-agent", "risk.evaluate"),
                ("order-policy", "policy.decide"),
                ("order-executor", "order.execute"),
            }
        )


def test_insufficient_inventory_is_persisted_as_rejected(tmp_path: Path) -> None:
    application = make_app(tmp_path)
    with TestClient(application) as client:
        response = client.post(
            "/orders",
            json=order_payload(sku="SKU-002", key="api-insufficient"),
        )

        assert response.status_code == 200
        result = response.json()
        assert result["status"] == OrderStatus.REJECTED.value
        lookup = client.get(f"/orders/{result['order_id']}")
        assert lookup.json()["status"] == OrderStatus.REJECTED.value
        assert application.state.inventory_repository.get("SKU-002").quantity == 0


def test_high_risk_order_is_persisted_for_manual_review(tmp_path: Path) -> None:
    application = make_app(tmp_path)
    with TestClient(application) as client:
        response = client.post(
            "/orders",
            json=order_payload(user_id="USER-003", key="api-high-risk"),
        )

        assert response.status_code == 200
        result = response.json()
        assert result["status"] == OrderStatus.MANUAL_REVIEW.value
        assert application.state.inventory_repository.get("SKU-001").quantity == 12


def test_duplicate_request_returns_one_order_and_deducts_inventory_once(
    tmp_path: Path,
) -> None:
    application = make_app(tmp_path)
    payload = order_payload(key="api-retry")
    with TestClient(application) as client:
        first = client.post("/orders", json=payload)
        second = client.post("/orders", json=payload)

        assert first.status_code == second.status_code == 200
        assert second.json()["idempotent_replay"] is True
        assert second.json()["order_id"] == first.json()["order_id"]
        assert second.json()["trace_id"] == first.json()["trace_id"]
        assert len(application.state.order_repository.list_all()) == 1
        assert application.state.inventory_repository.get("SKU-001").quantity == 11


def test_api_uses_stable_error_codes(tmp_path: Path) -> None:
    application = make_app(tmp_path)
    with TestClient(application) as client:
        missing = client.get("/orders/ORD-NOT-FOUND")
        invalid = client.post(
            "/orders",
            json={
                "user_id": "USER-001",
                "items": [],
                "total_amount": "0",
                "idempotency_key": "invalid",
            },
        )

        client.post("/orders", json=order_payload(key="api-conflict"))
        conflict = client.post(
            "/orders",
            json=order_payload(quantity=2, key="api-conflict"),
        )

    assert missing.status_code == 404
    assert missing.json()["error_code"] == "ORDER_NOT_FOUND"
    assert invalid.status_code == 422
    assert invalid.json()["error_code"] == "REQUEST_VALIDATION_ERROR"
    assert conflict.status_code == 409
    assert conflict.json()["error_code"] == "IDEMPOTENCY_CONFLICT"
    assert conflict.json()["trace_id"].startswith("TRACE-")
