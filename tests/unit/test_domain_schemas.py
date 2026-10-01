from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from order_agent_ops.domain import (
    ActionRiskLevel,
    AgentRunRecord,
    ApprovalRequest,
    DiagnosisResult,
    EvidenceRecord,
    IncidentRecord,
    IncidentStatus,
    InventoryCheckResult,
    InventoryStatus,
    OrderItem,
    OrderRecord,
    OrderRequest,
    OrderStatus,
    RecommendedAction,
    RemediationAction,
    RemediationStatus,
    RiskCheckResult,
    RiskLevel,
    RootCauseCandidate,
    RunStatus,
    ToolCallRecord,
)


NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


def valid_item(**overrides) -> OrderItem:
    values = {"sku": "SKU-001", "quantity": 2, "unit_price": Decimal("10.00")}
    values.update(overrides)
    return OrderItem(**values)


def valid_evidence() -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id="E-101",
        time=NOW,
        source={"type": "trace", "query_ref": "trace-order-882"},
        object={"id": "inventory-agent", "version": "v2.1"},
        fact={"observation": "Inventory latency exceeded baseline", "duration_ms": 2350},
    )


def test_legal_core_examples_validate() -> None:
    request = OrderRequest(
        user_id="USER-001",
        items=[valid_item()],
        total_amount=Decimal("20.00"),
        idempotency_key="order-attempt-001",
    )
    order = OrderRecord(
        request_id="REQ-001",
        order_id="ORD-001",
        trace_id="TRACE-001",
        user_id=request.user_id,
        items=request.items,
        total_amount=request.total_amount,
        idempotency_key=request.idempotency_key,
        status=OrderStatus.RECEIVED,
        created_at=NOW,
        updated_at=NOW,
    )
    inventory = InventoryCheckResult(
        sku="SKU-001",
        requested_quantity=2,
        status=InventoryStatus.SUFFICIENT,
        available_quantity=8,
        reason="Stock snapshot is sufficient",
        evidence_ids=["E-101"],
    )
    risk = RiskCheckResult(
        user_id="USER-001",
        risk_score=0.2,
        risk_level=RiskLevel.LOW,
        reason="Stable simulated profile",
        evidence_ids=["E-102"],
    )

    assert order.status is OrderStatus.RECEIVED
    assert inventory.status is InventoryStatus.SUFFICIENT
    assert risk.risk_level is RiskLevel.LOW
    assert valid_evidence().evidence_id == "E-101"


def test_legal_audit_and_operations_examples_validate() -> None:
    agent_run = AgentRunRecord(
        run_id="RUN-001",
        trace_id="TRACE-001",
        agent_name="inventory-agent",
        agent_version="v2.1",
        prompt_version="inventory-v1",
        model_provider="mock",
        model_name="mock-order-model",
        status=RunStatus.SUCCESS,
        started_at=NOW,
        ended_at=NOW,
        duration_ms=12.5,
    )
    tool_call = ToolCallRecord(
        call_id="CALL-001",
        trace_id="TRACE-001",
        tool_name="query_observability",
        tool_version="v1",
        status=RunStatus.SUCCESS,
        started_at=NOW,
        ended_at=NOW,
        duration_ms=8.0,
    )
    incident = IncidentRecord(
        incident_id="INC-001",
        status=IncidentStatus.OPEN,
        object_id="inventory-agent",
        rule="inventory_p95_latency",
        summary="Inventory latency exceeded threshold",
        occurred_at=NOW,
        source_refs=["metric-inventory-p95"],
    )
    approval = ApprovalRequest(
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
    )
    remediation = RemediationAction(
        action_id="ACTION-001",
        action="rollback",
        target="inventory-agent",
        target_version="v2.0",
        risk_level=ActionRiskLevel.HIGH,
        requires_approval=True,
        reason_evidence_ids=["E-101"],
        idempotency_key="rollback-inventory-v2.0",
        approval_id=approval.approval_id,
        requested_at=NOW,
    )

    assert agent_run.status is RunStatus.SUCCESS
    assert tool_call.trace_id == agent_run.trace_id
    assert incident.status is IncidentStatus.OPEN
    assert remediation.status is RemediationStatus.PENDING


@pytest.mark.parametrize("quantity", [0, -1])
def test_non_positive_item_quantity_is_rejected(quantity: int) -> None:
    with pytest.raises(ValidationError):
        valid_item(quantity=quantity)


def test_negative_amount_is_rejected() -> None:
    with pytest.raises(ValidationError):
        OrderRequest(
            user_id="USER-001",
            items=[valid_item()],
            total_amount=Decimal("-0.01"),
            idempotency_key="order-attempt-001",
        )


@pytest.mark.parametrize("confidence", [-0.01, 1.01])
def test_invalid_root_cause_confidence_is_rejected(confidence: float) -> None:
    with pytest.raises(ValidationError):
        RootCauseCandidate(
            cause="Deployment regression",
            confidence=confidence,
            evidence_ids=["E-101"],
        )


def test_high_risk_action_without_approval_is_rejected() -> None:
    with pytest.raises(ValidationError, match="must require approval"):
        RecommendedAction(
            action="rollback",
            target="inventory-agent",
            risk_level=ActionRiskLevel.HIGH,
            requires_approval=False,
            evidence_ids=["E-101"],
        )

    with pytest.raises(ValidationError, match="must require approval"):
        ApprovalRequest(
            approval_id="APPROVAL-001",
            action="rollback",
            target="inventory-agent",
            target_version="v2.0",
            risk_level=ActionRiskLevel.HIGH,
            requires_approval=False,
            impact_scope="Inventory checks",
            reason_evidence_ids=["E-101"],
            requested_by="ops-guardian",
            requested_at=NOW,
        )

    with pytest.raises(ValidationError, match="must require approval"):
        RemediationAction(
            action_id="ACTION-001",
            action="rollback",
            target="inventory-agent",
            target_version="v2.0",
            risk_level=ActionRiskLevel.HIGH,
            requires_approval=False,
            reason_evidence_ids=["E-101"],
            idempotency_key="rollback-inventory-v2.0",
            requested_at=NOW,
        )


def test_evidence_has_exactly_five_top_level_fields() -> None:
    evidence = valid_evidence()

    assert set(EvidenceRecord.model_fields) == {
        "evidence_id",
        "time",
        "source",
        "object",
        "fact",
    }
    assert set(evidence.model_dump()) == set(EvidenceRecord.model_fields)

    with pytest.raises(ValidationError):
        EvidenceRecord(
            **evidence.model_dump(),
            trace_id="TRACE-001",
        )


def test_diagnosis_links_facts_and_candidates_to_evidence() -> None:
    diagnosis = DiagnosisResult(
        incident_id="INC-001",
        facts=[{"statement": "P95 exceeded threshold", "evidence_ids": ["E-101"]}],
        root_cause_candidates=[
            {
                "cause": "Inventory v2.1 deployment regression",
                "confidence": 0.82,
                "evidence_ids": ["E-101", "E-102"],
            }
        ],
        missing_information=[],
        recommended_actions=[
            {
                "action": "rollback",
                "target": "inventory-agent",
                "risk_level": "high",
                "requires_approval": True,
                "evidence_ids": ["E-101", "E-102"],
            }
        ],
    )

    assert diagnosis.facts[0].evidence_ids == ["E-101"]
    assert diagnosis.root_cause_candidates[0].confidence == 0.82


def test_arbitrary_status_and_naive_time_are_rejected() -> None:
    with pytest.raises(ValidationError):
        OrderRecord(
            request_id="REQ-001",
            order_id="ORD-001",
            trace_id="TRACE-001",
            user_id="USER-001",
            items=[valid_item()],
            total_amount=Decimal("20.00"),
            idempotency_key="order-attempt-001",
            status="NOT_A_REAL_STATUS",
            created_at=NOW,
            updated_at=NOW,
        )

    with pytest.raises(ValidationError):
        EvidenceRecord(
            evidence_id="E-101",
            time=datetime(2026, 9, 27, 12, 0),
            source={"type": "trace", "query_ref": "trace-order-882"},
            object={"id": "inventory-agent", "version": "v2.1"},
            fact={"observation": "Inventory latency exceeded baseline"},
        )
