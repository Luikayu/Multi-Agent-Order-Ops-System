"""Approval and remediation action contracts."""

from typing import Any

from pydantic import AwareDatetime, Field, model_validator

from order_agent_ops.domain.base import (
    ActionId,
    ApprovalId,
    DomainModel,
    EvidenceId,
    IncidentId,
    NonEmptyString,
)
from order_agent_ops.domain.enums import (
    ActionRiskLevel,
    ApprovalStatus,
    RemediationStatus,
)


class ApprovalRequest(DomainModel):
    approval_id: ApprovalId
    action: NonEmptyString
    target: NonEmptyString
    target_version: str | None = None
    risk_level: ActionRiskLevel
    requires_approval: bool
    impact_scope: NonEmptyString
    reason_evidence_ids: list[EvidenceId] = Field(min_length=1)
    status: ApprovalStatus = ApprovalStatus.PENDING
    requested_by: NonEmptyString
    requested_at: AwareDatetime
    decided_by: str | None = None
    decided_at: AwareDatetime | None = None
    approval_token_hash: str | None = None
    token_expires_at: AwareDatetime | None = None
    token_used_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def high_risk_requires_approval(self) -> "ApprovalRequest":
        if self.risk_level in {ActionRiskLevel.HIGH, ActionRiskLevel.CRITICAL}:
            if not self.requires_approval:
                raise ValueError("High-risk actions must require approval")
        return self


class RemediationAction(DomainModel):
    action_id: ActionId
    action: NonEmptyString
    target: NonEmptyString
    target_version: str | None = None
    risk_level: ActionRiskLevel
    requires_approval: bool
    reason_evidence_ids: list[EvidenceId] = Field(min_length=1)
    idempotency_key: NonEmptyString
    approval_id: ApprovalId | None = None
    incident_id: IncidentId | None = None
    status: RemediationStatus = RemediationStatus.PENDING
    requested_at: AwareDatetime
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    before_state: dict[str, Any] | None = None
    after_state: dict[str, Any] | None = None
    error_code: str | None = None

    @model_validator(mode="after")
    def high_risk_requires_approval(self) -> "RemediationAction":
        if self.risk_level in {ActionRiskLevel.HIGH, ActionRiskLevel.CRITICAL}:
            if not self.requires_approval:
                raise ValueError("High-risk actions must require approval")
        return self
