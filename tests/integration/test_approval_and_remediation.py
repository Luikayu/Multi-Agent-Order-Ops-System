from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from order_agent_ops.domain.enums import ActionRiskLevel, RemediationStatus
from order_agent_ops.faults import FaultController, FaultTarget
from order_agent_ops.main import create_app
from order_agent_ops.models import MockModelProvider
from order_agent_ops.ops.approval_service import (
    ApprovalBindingError,
    ApprovalService,
)
from order_agent_ops.ops.evidence_store import EvidenceDraft
from order_agent_ops.ops.remediation_executor import (
    ExecuteRemediationRequest,
    RemediationExecutor,
)
from order_agent_ops.storage.repositories import (
    ApprovalRepository,
    AuditRepository,
    RemediationActionRepository,
)


NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


def save_reason_evidence(application) -> str:
    return application.state.evidence_store.save(
        EvidenceDraft(
            time=NOW,
            source={"type": "deployment", "query_ref": "CTX-001:deployment"},
            object={"id": "inventory-agent", "version": "v2.1"},
            fact={"observation": "inventory-agent v2.1 was deployed"},
        )
    ).evidence_id


def remediation_request(
    evidence_id: str,
    *,
    key: str,
    approval_id: str | None = None,
    token: str | None = None,
) -> ExecuteRemediationRequest:
    return ExecuteRemediationRequest(
        action="rollback",
        target="inventory-agent",
        target_version="v2.0",
        approval_id=approval_id,
        approval_token=token,
        reason_evidence_ids=[evidence_id],
        idempotency_key=key,
    )


def test_approval_gate_single_use_binding_rollback_and_audit(tmp_path: Path) -> None:
    controller = FaultController.from_yaml()
    controller.activate("inventory_v21_latency")
    application = create_app(
        database_path=tmp_path / "approval.sqlite3",
        provider=MockModelProvider(),
        fault_controller=controller,
    )

    with TestClient(application):
        evidence_id = save_reason_evidence(application)

        rejected = application.state.execute_remediation.execute(
            remediation_request(evidence_id, key="without-approval")
        )
        assert rejected.status is RemediationStatus.REJECTED
        assert rejected.error_code == "HUMAN_APPROVAL_REQUIRED"
        assert (
            controller.active_scenario_name(FaultTarget.INVENTORY_ADAPTER)
            == "inventory_v21_latency"
        )

        approval = application.state.approval_service.create_request(
            action="rollback",
            target="inventory-agent",
            target_version="v2.0",
            risk_level=ActionRiskLevel.HIGH,
            impact_scope="Inventory checks",
            reason_evidence_ids=[evidence_id],
            requested_by="ops-guardian-agent",
        )
        decision = application.state.approval_service.decide(
            approval.approval_id,
            approved=True,
            decided_by="human-operator",
        )
        assert decision.approval_token is not None

        with pytest.raises(ApprovalBindingError, match="does not match"):
            application.state.approval_service.validate_and_consume(
                approval_id=approval.approval_id,
                approval_token=decision.approval_token,
                action="rollback",
                target="risk-agent",
                target_version="v2.0",
            )

        succeeded = application.state.execute_remediation.execute(
            remediation_request(
                evidence_id,
                key="approved-rollback",
                approval_id=approval.approval_id,
                token=decision.approval_token,
            )
        )
        replayed = application.state.execute_remediation.execute(
            remediation_request(
                evidence_id,
                key="approved-rollback",
                approval_id=approval.approval_id,
                token=decision.approval_token,
            )
        )
        reused = application.state.execute_remediation.execute(
            remediation_request(
                evidence_id,
                key="reused-token",
                approval_id=approval.approval_id,
                token=decision.approval_token,
            )
        )

    assert succeeded.status is RemediationStatus.SUCCEEDED
    assert succeeded.before_state["version"] == "v2.1"
    assert succeeded.after_state["version"] == "v2.0"
    assert replayed.action_id == succeeded.action_id
    assert reused.status is RemediationStatus.REJECTED
    assert reused.error_code == "HUMAN_APPROVAL_REQUIRED"
    assert (
        controller.active_scenario_name(FaultTarget.INVENTORY_ADAPTER)
        == "inventory_v20_normal"
    )
    action_events = application.state.audit_repository.list_for_entity(
        "remediation_action", succeeded.action_id
    )
    assert [event.event_type for event in action_events] == [
        "remediation_attempt",
        "remediation_idempotent_replay",
    ]
    rejected_events = application.state.audit_repository.list_for_entity(
        "remediation_action", rejected.action_id
    )
    assert rejected_events[0].details["status"] == "rejected"
    approval_events = application.state.audit_repository.list_for_entity(
        "approval", approval.approval_id
    )
    assert [event.event_type for event in approval_events] == [
        "approval_requested",
        "approval_decided",
        "approval_token_consumed",
    ]


def test_expired_approval_token_is_rejected_and_audited(tmp_path: Path) -> None:
    controller = FaultController.from_yaml()
    controller.activate("inventory_v21_latency")
    application = create_app(
        database_path=tmp_path / "expired-approval.sqlite3",
        provider=MockModelProvider(),
        fault_controller=controller,
    )
    clock = [NOW]

    with TestClient(application):
        evidence_id = save_reason_evidence(application)
        approval_service = ApprovalService(
            ApprovalRepository(application.state.database),
            application.state.evidence_store,
            AuditRepository(application.state.database),
            token_ttl=timedelta(minutes=1),
            clock=lambda: clock[0],
            token_factory=lambda: "APPROVAL-TOKEN-FIXED",
        )
        executor = RemediationExecutor(
            RemediationActionRepository(application.state.database),
            application.state.incident_repository,
            approval_service,
            controller,
            AuditRepository(application.state.database),
        )
        approval = approval_service.create_request(
            action="rollback",
            target="inventory-agent",
            target_version="v2.0",
            risk_level=ActionRiskLevel.HIGH,
            impact_scope="Inventory checks",
            reason_evidence_ids=[evidence_id],
            requested_by="ops-guardian-agent",
        )
        decision = approval_service.decide(
            approval.approval_id,
            approved=True,
            decided_by="human-operator",
        )
        clock[0] = NOW + timedelta(minutes=2)

        result = executor.execute(
            remediation_request(
                evidence_id,
                key="expired-token",
                approval_id=approval.approval_id,
                token=decision.approval_token,
            )
        )

    assert result.status is RemediationStatus.REJECTED
    assert result.error_code == "HUMAN_APPROVAL_REQUIRED"
    assert (
        controller.active_scenario_name(FaultTarget.INVENTORY_ADAPTER)
        == "inventory_v21_latency"
    )
    events = application.state.audit_repository.list_for_entity(
        "remediation_action", result.action_id
    )
    assert events[0].details["detail"] == "Approval token has expired"
