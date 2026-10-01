from datetime import datetime, timedelta, timezone
from pathlib import Path

from order_agent_ops.domain.agents import AgentRunRecord, ToolCallRecord
from order_agent_ops.domain.enums import (
    ActionRiskLevel,
    IncidentStatus,
    OrderStatus,
    RunStatus,
)
from order_agent_ops.domain.orders import OrderItem, OrderRecord
from order_agent_ops.models.gateway import ModelCallRecord, ModelCallStatus
from order_agent_ops.ops.detector import AnomalyDetector
from order_agent_ops.ops.rules import (
    AnomalyThresholds,
    DangerousActionObservation,
    HandoffObservation,
)
from order_agent_ops.storage.database import Database
from order_agent_ops.storage.repositories import IncidentRepository
from order_agent_ops.telemetry.metrics import MetricRegistry


NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


def make_detector(
    tmp_path: Path,
    *,
    metrics: MetricRegistry | None = None,
    order_provider=None,
    thresholds: AnomalyThresholds | None = None,
) -> tuple[AnomalyDetector, IncidentRepository]:
    database = Database(tmp_path / "detector.sqlite3")
    database.initialize()
    incidents = IncidentRepository(database)
    detector = AnomalyDetector(
        metrics or MetricRegistry(),
        incidents,
        order_provider=order_provider,
        thresholds=thresholds,
        clock=lambda: NOW,
    )
    return detector, incidents


def agent_run(
    sequence: int,
    status: RunStatus,
    *,
    retry_count: int = 0,
) -> AgentRunRecord:
    started_at = NOW + timedelta(seconds=sequence)
    return AgentRunRecord(
        run_id=f"RUN-{sequence:03d}",
        trace_id=f"TRACE-{sequence:03d}",
        agent_name="inventory-agent",
        agent_version="v2.1",
        prompt_version="inventory-v1",
        model_provider="mock",
        model_name="mock-order-model",
        status=status,
        started_at=started_at,
        ended_at=started_at + timedelta(milliseconds=10),
        duration_ms=10,
        retry_count=retry_count,
        error_type=None if status is RunStatus.SUCCESS else "TestError",
    )


def tool_call(sequence: int, status: RunStatus) -> ToolCallRecord:
    started_at = NOW + timedelta(seconds=sequence)
    return ToolCallRecord(
        call_id=f"CALL-{sequence:03d}",
        trace_id=f"TRACE-TOOL-{sequence:03d}",
        tool_name="query_observability",
        tool_version="v1",
        status=status,
        started_at=started_at,
        ended_at=started_at + timedelta(milliseconds=5),
        duration_ms=5,
        error_type=None if status is RunStatus.SUCCESS else "TestError",
    )


def pending_order(sequence: int) -> OrderRecord:
    return OrderRecord(
        request_id=f"REQ-{sequence:03d}",
        order_id=f"ORD-{sequence:03d}",
        trace_id=f"TRACE-ORDER-{sequence:03d}",
        user_id="USER-001",
        items=[OrderItem(sku="SKU-001", quantity=1, unit_price="399.00")],
        total_amount="399.00",
        idempotency_key=f"pending-{sequence}",
        status=OrderStatus.PENDING,
        created_at=NOW,
        updated_at=NOW,
    )


def model_call(
    sequence: int,
    status: ModelCallStatus,
    *,
    retry_count: int,
) -> ModelCallRecord:
    started_at = NOW + timedelta(seconds=sequence)
    return ModelCallRecord(
        call_id=f"MODEL-CALL-{sequence:03d}",
        trace_id="TRACE-MODEL-001",
        task_type="inventory_agent",
        provider="mock",
        model_name="mock-order-model",
        prompt_version="inventory-v1",
        agent_run_id="RUN-MODEL-001",
        attempt=sequence,
        retry_count=retry_count,
        started_at=started_at,
        ended_at=started_at + timedelta(milliseconds=5),
        duration_ms=5,
        status=status,
        error_type="InvalidModelResponseError",
    )


def test_normal_metrics_do_not_create_false_positive(tmp_path: Path) -> None:
    metrics = MetricRegistry()
    for duration in (120, 150, 180):
        metrics.record("inventory-agent.inventory.check", duration, success=True)
        metrics.record("order-workflow.order.request", duration + 100, success=True)
    detector, incidents = make_detector(tmp_path, metrics=metrics)

    assert detector.detect() == []
    assert incidents.list_all() == []


def test_inventory_latency_creates_one_locatable_incident_and_merges_order_signal(
    tmp_path: Path,
) -> None:
    metrics = MetricRegistry()
    metrics.record("inventory-agent.inventory.check", 2300, success=True)
    metrics.record("order-workflow.order.request", 2400, success=True)
    detector, incidents = make_detector(tmp_path, metrics=metrics)

    detected = detector.detect()

    assert len(detected) == 1
    incident = detected[0]
    assert incident.status is IncidentStatus.OPEN
    assert incident.object_id == "inventory-agent"
    assert incident.rule == "inventory_p95_latency"
    assert incident.occurred_at == NOW
    assert {reference.split(":", 2)[1] for reference in incident.source_refs} == {
        "inventory-agent.inventory.check",
        "order-workflow.order.request",
    }
    assert incidents.list_all() == [incident]


def test_repeated_detection_reuses_active_incident(tmp_path: Path) -> None:
    metrics = MetricRegistry()
    metrics.record("inventory-agent.inventory.check", 2300, success=True)
    detector, incidents = make_detector(tmp_path, metrics=metrics)

    first = detector.detect()[0]
    second = detector.detect()[0]

    assert second.incident_id == first.incident_id
    assert len(incidents.list_all()) == 1


def test_order_latency_without_inventory_signal_is_reported(tmp_path: Path) -> None:
    metrics = MetricRegistry()
    metrics.record("order-workflow.order.request", 2100, success=True)
    detector, _ = make_detector(tmp_path, metrics=metrics)

    incident = detector.detect()[0]

    assert incident.object_id == "order-workflow"
    assert incident.rule == "order_p95_latency"


def test_three_trailing_agent_and_tool_failures_are_detected(tmp_path: Path) -> None:
    detector, _ = make_detector(tmp_path)
    two_agent_failures = [
        agent_run(1, RunStatus.ERROR),
        agent_run(2, RunStatus.TIMEOUT),
    ]

    assert detector.detect(agent_runs=two_agent_failures) == []

    detected = detector.detect(
        agent_runs=[*two_agent_failures, agent_run(3, RunStatus.ERROR)],
        tool_calls=[
            tool_call(1, RunStatus.ERROR),
            tool_call(2, RunStatus.TIMEOUT),
            tool_call(3, RunStatus.ERROR),
        ],
    )

    consecutive = {
        incident.object_id: incident
        for incident in detected
        if incident.rule == "consecutive_failures"
    }
    assert set(consecutive) == {"inventory-agent", "query_observability"}
    assert len(consecutive["inventory-agent"].source_refs) == 3
    assert len(consecutive["query_observability"].source_refs) == 3


def test_pending_count_must_exceed_threshold(tmp_path: Path) -> None:
    orders = [pending_order(index) for index in range(1, 5)]
    visible_orders = orders[:3]
    detector, _ = make_detector(
        tmp_path,
        order_provider=lambda: visible_orders,
        thresholds=AnomalyThresholds(pending_orders=3),
    )

    assert detector.detect() == []

    visible_orders.append(orders[3])
    incident = detector.detect()[0]

    assert incident.rule == "pending_order_count"
    assert incident.object_id == "order-workflow"
    assert len(incident.source_refs) == 4


def test_invalid_output_retry_handoff_and_unapproved_action_rules(
    tmp_path: Path,
) -> None:
    detector, _ = make_detector(tmp_path)

    detected = detector.detect(
        agent_runs=[agent_run(1, RunStatus.INVALID_OUTPUT, retry_count=1)],
        handoffs=[
            HandoffObservation(
                handoff_id="HANDOFF-001",
                source_agent="order-coordinator-agent",
                target_agent="inventory-agent",
                trace_id=None,
                occurred_at=NOW,
            )
        ],
        dangerous_action_attempts=[
            DangerousActionObservation(
                attempt_id="ATTEMPT-001",
                action="rollback",
                target="inventory-agent",
                risk_level=ActionRiskLevel.HIGH,
                approved=False,
                occurred_at=NOW,
            )
        ],
    )

    assert {incident.rule for incident in detected} == {
        "schema_validation_failure",
        "max_retries_reached",
        "handoff_missing_trace_id",
        "dangerous_action_without_approval",
    }
    assert all(incident.source_refs for incident in detected)


def test_model_call_records_detect_schema_failure_and_exhausted_retry(
    tmp_path: Path,
) -> None:
    detector, _ = make_detector(tmp_path)
    calls = [
        model_call(1, ModelCallStatus.INVALID_OUTPUT, retry_count=0),
        model_call(2, ModelCallStatus.INVALID_OUTPUT, retry_count=1),
    ]

    detected = detector.detect(model_calls=calls)

    by_rule = {incident.rule: incident for incident in detected}
    assert set(by_rule) == {"schema_validation_failure", "max_retries_reached"}
    assert by_rule["schema_validation_failure"].object_id == "inventory-agent"
    assert len(by_rule["schema_validation_failure"].source_refs) == 2
    assert by_rule["max_retries_reached"].source_refs == [
        "model-call:MODEL-CALL-002"
    ]
