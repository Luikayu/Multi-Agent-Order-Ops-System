from pathlib import Path

from fastapi.testclient import TestClient

from order_agent_ops.faults import FaultController
from order_agent_ops.domain.enums import OrderStatus
from order_agent_ops.main import create_app
from order_agent_ops.models import MockModelProvider


def test_inventory_latency_fault_produces_one_deduplicated_incident(
    tmp_path: Path,
) -> None:
    controller = FaultController.from_yaml()
    application = create_app(
        database_path=tmp_path / "anomaly-detection.sqlite3",
        provider=MockModelProvider(),
        fault_controller=controller,
    )
    payload = {
        "user_id": "USER-001",
        "items": [
            {"sku": "SKU-001", "quantity": 1, "unit_price": "399.00"}
        ],
        "total_amount": "399.00",
        "idempotency_key": "detect-inventory-latency",
    }

    with TestClient(application) as client:
        with controller.activated("inventory_v21_latency"):
            response = client.post("/orders", json=payload)

        first = application.state.anomaly_detector.detect()
        second = application.state.anomaly_detector.detect()

    assert response.status_code == 200
    assert len(first) == len(second) == 1
    assert first[0].incident_id == second[0].incident_id
    assert first[0].object_id == "inventory-agent"
    assert first[0].rule == "inventory_p95_latency"
    assert len(application.state.incident_repository.list_all()) == 1
    assert any(
        "inventory-agent.inventory.check" in reference
        for reference in first[0].source_refs
    )


def test_invalid_model_output_is_detected_from_gateway_records(
    tmp_path: Path,
) -> None:
    controller = FaultController.from_yaml()
    application = create_app(
        database_path=tmp_path / "invalid-output-detection.sqlite3",
        provider=MockModelProvider(),
        fault_controller=controller,
    )
    payload = {
        "user_id": "USER-001",
        "items": [
            {"sku": "SKU-001", "quantity": 1, "unit_price": "399.00"}
        ],
        "total_amount": "399.00",
        "idempotency_key": "detect-invalid-output",
    }

    with TestClient(application) as client:
        with controller.activated("inventory_agent_invalid_output"):
            response = client.post("/orders", json=payload)
        incidents = application.state.anomaly_detector.detect()

    assert response.json()["status"] == OrderStatus.PENDING.value
    by_rule = {incident.rule: incident for incident in incidents}
    assert set(by_rule) == {"schema_validation_failure", "max_retries_reached"}
    assert all(incident.object_id == "inventory-agent" for incident in incidents)
    assert any(
        reference.startswith("model-call:")
        for reference in by_rule["schema_validation_failure"].source_refs
    )
