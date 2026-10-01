import time
from pathlib import Path

from fastapi.testclient import TestClient

from order_agent_ops.domain.enums import OrderStatus
from order_agent_ops.faults import FaultController, FaultTarget
from order_agent_ops.main import create_app
from order_agent_ops.models import MockModelProvider


def order_payload(key: str) -> dict[str, object]:
    return {
        "user_id": "USER-001",
        "items": [
            {"sku": "SKU-001", "quantity": 1, "unit_price": "399.00"}
        ],
        "total_amount": "399.00",
        "idempotency_key": key,
    }


def test_inventory_timeout_makes_order_pending_then_deactivation_restores_flow(
    tmp_path: Path,
) -> None:
    controller = FaultController.from_yaml()
    application = create_app(
        database_path=tmp_path / "fault-timeout.sqlite3",
        provider=MockModelProvider(),
        fault_controller=controller,
    )

    with TestClient(application) as client:
        with controller.activated("inventory_forced_timeout"):
            pending = client.post("/orders", json=order_payload("fault-pending"))

        completed = client.post("/orders", json=order_payload("fault-restored"))

    assert pending.status_code == 200
    assert pending.json()["status"] == OrderStatus.PENDING.value
    assert completed.status_code == 200
    assert completed.json()["status"] == OrderStatus.COMPLETED.value
    assert controller.active_scenario(FaultTarget.INVENTORY_ADAPTER) is None


def test_inventory_v21_is_obviously_slower_and_automatically_restored(
    tmp_path: Path,
) -> None:
    controller = FaultController.from_yaml()
    application = create_app(
        database_path=tmp_path / "fault-latency.sqlite3",
        provider=MockModelProvider(),
        fault_controller=controller,
    )

    with TestClient(application) as client:
        started = time.perf_counter()
        with controller.activated("inventory_v21_latency"):
            slow = client.post("/orders", json=order_payload("fault-slow"))
        slow_seconds = time.perf_counter() - started

        started = time.perf_counter()
        normal = client.post("/orders", json=order_payload("fault-normal"))
        normal_seconds = time.perf_counter() - started

    assert slow.status_code == normal.status_code == 200
    assert slow.json()["status"] == normal.json()["status"] == "COMPLETED"
    assert slow_seconds >= 2.2
    assert slow_seconds - normal_seconds >= 2.0
    assert controller.active_scenario(FaultTarget.INVENTORY_ADAPTER) is None
