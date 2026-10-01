from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from order_agent_ops.domain.enums import (
    ActionRiskLevel,
    IncidentStatus,
    OrderStatus,
    RemediationStatus,
)
from order_agent_ops.domain.incidents import IncidentRecord
from order_agent_ops.faults import FaultController, FaultTarget
from order_agent_ops.main import create_app
from order_agent_ops.models import MockModelProvider
from order_agent_ops.ops.approval_service import (
    ApprovalDecisionResult,
    ApprovalError,
)
from order_agent_ops.ops.evidence_store import EvidenceDraft
from order_agent_ops.ops.remediation_executor import ExecuteRemediationRequest
from order_agent_ops.ops.verification_service import (
    VerificationProbeResult,
    VerificationService,
)


NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


def order_payload(key: str) -> dict[str, object]:
    return {
        "user_id": "USER-001",
        "items": [
            {
                "sku": "SKU-001",
                "quantity": 1,
                "unit_price": "399.00",
            }
        ],
        "total_amount": "399.00",
        "idempotency_key": key,
    }


def create_incident(application, incident_id: str) -> IncidentRecord:
    incident = IncidentRecord(
        incident_id=incident_id,
        status=IncidentStatus.DIAGNOSED,
        object_id="inventory-agent",
        rule="inventory_agent_p95_latency",
        summary="Inventory Agent v2.1 is unhealthy",
        occurred_at=NOW,
        source_refs=["TRACE-failed-order"],
    )
    return application.state.incident_repository.create(incident)


def approve_rollback(application, evidence_id: str) -> ApprovalDecisionResult:
    approval = application.state.approval_service.create_request(
        action="rollback",
        target="inventory-agent",
        target_version="v2.0",
        risk_level=ActionRiskLevel.HIGH,
        impact_scope="Inventory checks",
        reason_evidence_ids=[evidence_id],
        requested_by="ops-guardian-agent",
    )
    return application.state.approval_service.decide(
        approval.approval_id,
        approved=True,
        decided_by="human-operator",
    )


def approve_replay(
    application,
    evidence_id: str,
    order_id: str,
) -> ApprovalDecisionResult:
    approval = application.state.approval_service.create_request(
        action="replay",
        target=order_id,
        target_version=None,
        risk_level=ActionRiskLevel.HIGH,
        impact_scope="Retry one pending order",
        reason_evidence_ids=[evidence_id],
        requested_by="human-operator",
    )
    return application.state.approval_service.decide(
        approval.approval_id,
        approved=True,
        decided_by="human-operator",
    )


def test_rollback_verification_and_pending_order_replay_complete_once(
    tmp_path: Path,
) -> None:
    controller = FaultController.from_yaml()
    controller.activate("inventory_forced_timeout")
    application = create_app(
        database_path=tmp_path / "recovery.sqlite3",
        provider=MockModelProvider(),
        fault_controller=controller,
    )

    with TestClient(application) as client:
        pending_response = client.post(
            "/orders",
            json=order_payload("recovery-order"),
        )
        assert pending_response.status_code == 200
        pending = pending_response.json()
        assert pending["status"] == OrderStatus.PENDING.value
        assert application.state.inventory_repository.get("SKU-001").quantity == 12

        evidence = application.state.evidence_store.save(
            EvidenceDraft(
                time=NOW,
                source={"type": "deployment", "query_ref": "deployment-v2.1"},
                object={"id": "inventory-agent", "version": "v2.1"},
                fact={"observation": "v2.1 caused inventory timeouts"},
            )
        )
        incident = create_incident(application, "INC-recovery-loop")
        decision = approve_rollback(application, evidence.evidence_id)

        remediation = application.state.execute_remediation.execute(
            ExecuteRemediationRequest(
                action="rollback",
                target="inventory-agent",
                target_version="v2.0",
                approval_id=decision.approval.approval_id,
                approval_token=decision.approval_token,
                reason_evidence_ids=[evidence.evidence_id],
                idempotency_key="recovery-rollback",
                incident_id=incident.incident_id,
            )
        )
        assert remediation.status is RemediationStatus.SUCCEEDED
        assert remediation.before_state["version"] == "v2.1"
        assert remediation.after_state["version"] == "v2.0"
        assert (
            controller.active_scenario_name(FaultTarget.INVENTORY_ADAPTER)
            == "inventory_v20_normal"
        )

        verification = application.state.verification_service.verify(
            incident.incident_id
        )
        assert verification.success is True
        assert verification.incident.status is IncidentStatus.RESOLVED
        assert verification.inventory_p95_ms <= 2000
        assert verification.order_p95_ms <= 2000

        with pytest.raises(ApprovalError, match="approval token"):
            application.state.replay_service.replay(pending["order_id"])
        assert (
            application.state.order_repository.get(pending["order_id"]).status
            is OrderStatus.PENDING
        )
        replay_decision = approve_replay(
            application,
            evidence.evidence_id,
            pending["order_id"],
        )
        first_replay = application.state.replay_service.replay(
            pending["order_id"],
            approval_id=replay_decision.approval.approval_id,
            approval_token=replay_decision.approval_token,
        )
        quantity_after_first = application.state.inventory_repository.get(
            "SKU-001"
        ).quantity
        second_replay = application.state.replay_service.replay(
            pending["order_id"]
        )

    assert first_replay.order.status is OrderStatus.COMPLETED
    assert first_replay.order.order_id == pending["order_id"]
    assert first_replay.order.idempotency_key == "recovery-order"
    assert first_replay.idempotent_replay is False
    assert second_replay.idempotent_replay is True
    assert second_replay.order.order_id == first_replay.order.order_id
    assert quantity_after_first == 11
    assert application.state.inventory_repository.get("SKU-001").quantity == 11
    assert len(application.state.order_repository.list_all()) == 1
    replay_spans = application.state.trace_recorder.get_trace(
        first_replay.replay_trace_id
    )
    assert ("replay-service", "order.replay") in {
        (span.component, span.action) for span in replay_spans
    }


def test_failed_verification_escalates_and_does_not_resolve(tmp_path: Path) -> None:
    application = create_app(
        database_path=tmp_path / "verification-failure.sqlite3",
        provider=MockModelProvider(),
    )
    incident = create_incident(application, "INC-verification-failure")
    application.state.incident_repository.transition_status(
        incident.incident_id,
        IncidentStatus.REMEDIATING,
        trigger_component="test",
        reason="Prepare a deterministic failed verification",
    )
    service = VerificationService(
        application.state.incident_repository,
        lambda trace_id: VerificationProbeResult(
            trace_id=trace_id,
            success=False,
            inventory_latency_ms=2500,
            order_latency_ms=2600,
            detail="Inventory remained slow",
        ),
        application.state.trace_recorder,
    )

    result = service.verify(incident.incident_id)

    assert result.success is False
    assert result.incident.status is IncidentStatus.ESCALATED
    assert "unverified" in result.reason
    assert result.incident.status is not IncidentStatus.RESOLVED


def test_replay_with_persistent_fault_remains_pending_without_inventory_change(
    tmp_path: Path,
) -> None:
    controller = FaultController.from_yaml()
    controller.activate("inventory_forced_timeout")
    application = create_app(
        database_path=tmp_path / "replay-failure.sqlite3",
        provider=MockModelProvider(),
        fault_controller=controller,
    )

    with TestClient(application) as client:
        submitted = client.post(
            "/orders",
            json=order_payload("persistent-fault"),
        ).json()
        evidence = application.state.evidence_store.save(
            EvidenceDraft(
                time=NOW,
                source={"type": "trace", "query_ref": submitted["trace_id"]},
                object={"id": "inventory-agent", "version": "v2.1"},
                fact={"observation": "inventory check timed out"},
            )
        )
        replay_decision = approve_replay(
            application,
            evidence.evidence_id,
            submitted["order_id"],
        )
        replayed = application.state.replay_service.replay(
            submitted["order_id"],
            approval_id=replay_decision.approval.approval_id,
            approval_token=replay_decision.approval_token,
        )

    assert submitted["status"] == OrderStatus.PENDING.value
    assert replayed.order.status is OrderStatus.PENDING
    assert replayed.idempotent_replay is False
    assert application.state.inventory_repository.get("SKU-001").quantity == 12
    assert len(application.state.order_repository.list_all()) == 1
