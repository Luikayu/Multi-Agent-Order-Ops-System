"""Approval-gated executor for simulated remediation actions."""

from __future__ import annotations

from datetime import datetime, timezone
from threading import RLock
from uuid import uuid4

from pydantic import Field

from order_agent_ops.domain.approvals import RemediationAction
from order_agent_ops.domain.base import DomainModel, EvidenceId, IncidentId, NonEmptyString
from order_agent_ops.domain.enums import (
    ActionRiskLevel,
    IncidentStatus,
    RemediationStatus,
)
from order_agent_ops.faults import FaultController, FaultTarget
from order_agent_ops.ops.approval_service import ApprovalError, ApprovalService
from order_agent_ops.storage.repositories import (
    AuditRepository,
    IncidentRepository,
    RemediationActionRepository,
)


class ExecuteRemediationRequest(DomainModel):
    action: NonEmptyString
    target: NonEmptyString
    target_version: str | None = None
    approval_id: str | None = None
    approval_token: str | None = None
    reason_evidence_ids: list[EvidenceId] = Field(min_length=1)
    idempotency_key: NonEmptyString
    incident_id: IncidentId | None = None


class RemediationExecutor:
    """Perform only supported simulated state changes; never run system commands."""

    def __init__(
        self,
        actions: RemediationActionRepository,
        incidents: IncidentRepository,
        approvals: ApprovalService,
        faults: FaultController,
        audit: AuditRepository,
    ) -> None:
        self._actions = actions
        self._incidents = incidents
        self._approvals = approvals
        self._faults = faults
        self._audit = audit
        self._lock = RLock()

    def execute(self, request: ExecuteRemediationRequest) -> RemediationAction:
        with self._lock:
            existing = self._actions.get_by_idempotency_key(request.idempotency_key)
            if existing is not None:
                self._audit_attempt(existing, replay=True)
                return existing

            action = RemediationAction(
                action_id=f"ACTION-{uuid4().hex}",
                action=request.action,
                target=request.target,
                target_version=request.target_version,
                risk_level=ActionRiskLevel.HIGH,
                requires_approval=True,
                reason_evidence_ids=request.reason_evidence_ids,
                idempotency_key=request.idempotency_key,
                approval_id=request.approval_id,
                incident_id=request.incident_id,
                status=RemediationStatus.PENDING,
                requested_at=datetime.now(timezone.utc),
            )
            self._actions.create(action)

            try:
                self._approvals.validate_and_consume(
                    approval_id=request.approval_id,
                    approval_token=request.approval_token,
                    action=request.action,
                    target=request.target,
                    target_version=request.target_version,
                )
            except ApprovalError as error:
                rejected = action.model_copy(
                    update={
                        "status": RemediationStatus.REJECTED,
                        "finished_at": datetime.now(timezone.utc),
                        "error_code": error.error_code,
                    }
                )
                self._actions.update(rejected)
                self._audit_attempt(rejected, detail=str(error))
                return rejected

            if not self._is_supported(request):
                rejected = action.model_copy(
                    update={
                        "status": RemediationStatus.REJECTED,
                        "finished_at": datetime.now(timezone.utc),
                        "error_code": "UNSUPPORTED_REMEDIATION",
                    }
                )
                self._actions.update(rejected)
                self._audit_attempt(rejected, detail="Unsupported remediation")
                return rejected

            before_state = self._inventory_state()
            running = action.model_copy(
                update={
                    "status": RemediationStatus.RUNNING,
                    "started_at": datetime.now(timezone.utc),
                    "before_state": before_state,
                }
            )
            self._actions.update(running)
            try:
                if request.incident_id is not None:
                    incident = self._incidents.get(request.incident_id)
                    if incident is None:
                        raise ValueError(f"Incident not found: {request.incident_id}")
                    if incident.status is not IncidentStatus.DIAGNOSED:
                        raise ValueError(
                            "Remediation requires a DIAGNOSED incident"
                        )
                    self._incidents.transition_status(
                        incident.incident_id,
                        IncidentStatus.REMEDIATING,
                        trigger_component="remediation-executor",
                        reason="Approved simulated rollback started",
                        references=request.reason_evidence_ids,
                    )

                self._faults.deactivate_target(FaultTarget.INVENTORY_AGENT)
                self._faults.activate("inventory_v20_normal")
                succeeded = running.model_copy(
                    update={
                        "status": RemediationStatus.SUCCEEDED,
                        "finished_at": datetime.now(timezone.utc),
                        "after_state": self._inventory_state(),
                    }
                )
                self._actions.update(succeeded)
                self._audit_attempt(succeeded)
                return succeeded
            except Exception as error:
                failed = running.model_copy(
                    update={
                        "status": RemediationStatus.FAILED,
                        "finished_at": datetime.now(timezone.utc),
                        "after_state": self._inventory_state(),
                        "error_code": "REMEDIATION_FAILED",
                    }
                )
                self._actions.update(failed)
                self._audit_attempt(failed, detail=type(error).__name__)
                return failed

    @staticmethod
    def _is_supported(request: ExecuteRemediationRequest) -> bool:
        return (
            request.action == "rollback"
            and request.target == "inventory-agent"
            and request.target_version == "v2.0"
        )

    def _inventory_state(self) -> dict[str, str | None]:
        scenario_name = self._faults.active_scenario_name(
            FaultTarget.INVENTORY_ADAPTER
        )
        scenario = self._faults.active_scenario(FaultTarget.INVENTORY_ADAPTER)
        return {
            "object_id": "inventory-agent",
            "scenario": scenario_name,
            "version": scenario.component_version if scenario else "v2.0",
        }

    def _audit_attempt(
        self,
        action: RemediationAction,
        *,
        replay: bool = False,
        detail: str | None = None,
    ) -> None:
        self._audit.record(
            entity_type="remediation_action",
            entity_id=action.action_id,
            event_type=(
                "remediation_idempotent_replay" if replay else "remediation_attempt"
            ),
            details={
                "action": action.action,
                "target": action.target,
                "target_version": action.target_version,
                "status": action.status.value,
                "error_code": action.error_code,
                "approval_id": action.approval_id,
                "incident_id": action.incident_id,
                "detail": detail,
            },
        )
