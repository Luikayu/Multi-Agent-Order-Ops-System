from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from order_agent_ops.business.order_state_machine import InvalidOrderTransitionError
from order_agent_ops.domain import (
    ActionRiskLevel,
    AgentRunRecord,
    ApprovalRequest,
    EvidenceRecord,
    IncidentRecord,
    IncidentStatus,
    OrderItem,
    OrderRecord,
    OrderStatus,
    RemediationAction,
    RunStatus,
    ToolCallRecord,
)
from order_agent_ops.storage.database import Database
from order_agent_ops.storage.repositories import (
    AgentRunRepository,
    ApprovalRepository,
    AuditRepository,
    DuplicateRecordError,
    EvidenceRepository,
    IncidentRepository,
    InventoryRepository,
    OrderRepository,
    RemediationActionRepository,
    StateTransitionRequiredError,
    ToolCallRepository,
)


NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def database(tmp_path) -> Database:
    result = Database(tmp_path / "state" / "test.sqlite3")
    result.initialize()
    return result


def make_order() -> OrderRecord:
    return OrderRecord(
        request_id="REQ-001",
        order_id="ORD-001",
        trace_id="TRACE-001",
        user_id="USER-001",
        items=[OrderItem(sku="SKU-001", quantity=2, unit_price=Decimal("10.00"))],
        total_amount=Decimal("20.00"),
        idempotency_key="order-attempt-001",
        status=OrderStatus.RECEIVED,
        created_at=NOW,
        updated_at=NOW,
    )


def test_database_initialization_is_idempotent_and_complete(database: Database) -> None:
    database.initialize()
    database.initialize()

    with database.connection() as connection:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()

    assert {row["name"] for row in rows} == {
        "agent_runs",
        "approvals",
        "audit_events",
        "evidence",
        "incident_evidence",
        "incidents",
        "inventory",
        "orders",
        "purchase_intents",
        "remediation_actions",
        "tool_calls",
    }


def test_order_crud_transition_guard_and_audit(database: Database) -> None:
    orders = OrderRepository(database)
    audit = AuditRepository(database)
    order = make_order()

    orders.create(order)
    assert orders.get(order.order_id) == order
    assert orders.list_all() == [order]

    with pytest.raises(DuplicateRecordError):
        orders.create(order)

    revised = order.model_copy(
        update={
            "total_amount": Decimal("19.50"),
            "updated_at": NOW + timedelta(seconds=1),
        }
    )
    orders.update(revised)
    assert orders.get(order.order_id).total_amount == Decimal("19.50")

    bypass = revised.model_copy(update={"status": OrderStatus.COMPLETED})
    with pytest.raises(StateTransitionRequiredError):
        orders.update(bypass)

    checking = orders.transition_status(
        order.order_id,
        OrderStatus.CHECKING,
        trigger_component="order-workflow",
        reason="Required fields validated",
        references=["request-validation-001"],
    )
    assert checking.status is OrderStatus.CHECKING

    events = audit.list_for_entity("order", order.order_id)
    assert len(events) == 1
    assert events[0].event_type == "state_transition"
    assert events[0].details == {
        "request_id": "REQ-001",
        "order_id": "ORD-001",
        "trace_id": "TRACE-001",
        "from_status": "RECEIVED",
        "to_status": "CHECKING",
        "trigger_component": "order-workflow",
        "reason": "Required fields validated",
        "references": ["request-validation-001"],
    }

    with pytest.raises(InvalidOrderTransitionError):
        orders.transition_status(
            order.order_id,
            OrderStatus.COMPLETED,
            trigger_component="test",
            reason="Attempted state skip",
        )
    assert orders.get(order.order_id).status is OrderStatus.CHECKING
    assert len(audit.list_for_entity("order", order.order_id)) == 1

    orders.delete(order.order_id)
    assert orders.get(order.order_id) is None
    assert [event.event_type for event in audit.list_for_entity("order", order.order_id)] == [
        "state_transition",
        "deleted",
    ]


def test_incident_transition_is_audited_and_cannot_be_bypassed(
    database: Database,
) -> None:
    incidents = IncidentRepository(database)
    audit = AuditRepository(database)
    incident = IncidentRecord(
        incident_id="INC-001",
        status=IncidentStatus.OPEN,
        object_id="inventory-agent",
        rule="inventory_p95_latency",
        summary="Inventory latency exceeded threshold",
        occurred_at=NOW,
        source_refs=["metric-inventory-p95"],
    )
    incidents.create(incident)

    with pytest.raises(StateTransitionRequiredError):
        incidents.update(
            incident.model_copy(update={"status": IncidentStatus.RESOLVED})
        )

    investigating = incidents.transition_status(
        incident.incident_id,
        IncidentStatus.INVESTIGATING,
        trigger_component="anomaly-detector",
        reason="Latency rule triggered",
        references=["metric-inventory-p95"],
    )

    assert investigating.status is IncidentStatus.INVESTIGATING
    event = audit.list_for_entity("incident", incident.incident_id)[0]
    assert event.details["from_status"] == "OPEN"
    assert event.details["to_status"] == "INVESTIGATING"


def test_inventory_repository_supports_upsert_read_list_and_delete(
    database: Database,
) -> None:
    inventory = InventoryRepository(database)
    audit = AuditRepository(database)

    created = inventory.set_quantity("SKU-001", 10)
    updated = inventory.set_quantity("SKU-001", 7)

    assert created.quantity == 10
    assert updated.quantity == 7
    assert updated.created_at == created.created_at
    assert inventory.get("SKU-001") == updated
    assert inventory.list_all() == [updated]

    inventory.delete("SKU-001")
    assert inventory.get("SKU-001") is None
    assert [event.event_type for event in audit.list_for_entity("inventory", "SKU-001")] == [
        "quantity_set",
        "quantity_set",
        "deleted",
    ]


def test_all_json_model_repositories_round_trip_records(database: Database) -> None:
    records_and_repositories = [
        (
            AgentRunRepository(database),
            AgentRunRecord(
                run_id="RUN-001",
                trace_id="TRACE-001",
                agent_name="inventory-agent",
                agent_version="v2.0",
                prompt_version="inventory-v1",
                model_provider="mock",
                model_name="mock-order-model",
                status=RunStatus.SUCCESS,
                started_at=NOW,
                ended_at=NOW,
                duration_ms=15,
            ),
            "RUN-001",
        ),
        (
            ToolCallRepository(database),
            ToolCallRecord(
                call_id="CALL-001",
                trace_id="TRACE-001",
                tool_name="query_observability",
                tool_version="v1",
                status=RunStatus.SUCCESS,
                started_at=NOW,
                ended_at=NOW,
                duration_ms=8,
            ),
            "CALL-001",
        ),
        (
            EvidenceRepository(database),
            EvidenceRecord(
                evidence_id="E-101",
                time=NOW,
                source={"type": "metric", "query_ref": "metric-query-001"},
                object={"id": "inventory-agent", "version": "v2.1"},
                fact={"observation": "P95 exceeded threshold", "value": 2350},
            ),
            "E-101",
        ),
        (
            ApprovalRepository(database),
            ApprovalRequest(
                approval_id="APPROVAL-001",
                action="rollback",
                target="inventory-agent",
                target_version="v2.0",
                risk_level=ActionRiskLevel.HIGH,
                requires_approval=True,
                impact_scope="Inventory checks",
                reason_evidence_ids=["E-101"],
                requested_by="ops-guardian",
                requested_at=NOW,
            ),
            "APPROVAL-001",
        ),
        (
            RemediationActionRepository(database),
            RemediationAction(
                action_id="ACTION-001",
                action="rollback",
                target="inventory-agent",
                target_version="v2.0",
                risk_level=ActionRiskLevel.HIGH,
                requires_approval=True,
                reason_evidence_ids=["E-101"],
                idempotency_key="rollback-inventory-v2.0",
                approval_id="APPROVAL-001",
                requested_at=NOW,
            ),
            "ACTION-001",
        ),
    ]

    for repository, record, record_id in records_and_repositories:
        repository.create(record)
        assert repository.get(record_id) == record
        assert repository.list_all() == [record]
        repository.update(record)
        assert repository.get(record_id) == record
        repository.delete(record_id)
        assert repository.get(record_id) is None
