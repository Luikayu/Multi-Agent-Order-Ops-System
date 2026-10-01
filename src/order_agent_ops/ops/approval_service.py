"""Deterministic approval requests and single-use bounded tokens."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from order_agent_ops.domain.approvals import ApprovalRequest
from order_agent_ops.domain.base import DomainModel
from order_agent_ops.domain.enums import ActionRiskLevel, ApprovalStatus
from order_agent_ops.ops.evidence_store import EvidenceStore
from order_agent_ops.storage.repositories import (
    ApprovalRepository,
    AuditRepository,
    RecordNotFoundError,
)


class ApprovalError(ValueError):
    error_code = "HUMAN_APPROVAL_REQUIRED"


class ApprovalExpiredError(ApprovalError):
    pass


class ApprovalTokenUsedError(ApprovalError):
    pass


class ApprovalBindingError(ApprovalError):
    pass


class ApprovalDecisionResult(DomainModel):
    approval: ApprovalRequest
    approval_token: str | None = None


class ApprovalService:
    """Issue approval tokens bound to one action, target, and version."""

    def __init__(
        self,
        approvals: ApprovalRepository,
        evidence_store: EvidenceStore,
        audit: AuditRepository,
        *,
        token_ttl: timedelta = timedelta(minutes=10),
        clock: Callable[[], datetime] | None = None,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        if token_ttl.total_seconds() <= 0:
            raise ValueError("Approval token TTL must be positive")
        self._approvals = approvals
        self._evidence_store = evidence_store
        self._audit = audit
        self._token_ttl = token_ttl
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._token_factory = token_factory or (
            lambda: f"APPROVAL-TOKEN-{secrets.token_urlsafe(32)}"
        )

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def create_request(
        self,
        *,
        action: str,
        target: str,
        target_version: str | None,
        risk_level: ActionRiskLevel,
        impact_scope: str,
        reason_evidence_ids: list[str],
        requested_by: str,
    ) -> ApprovalRequest:
        missing = [
            evidence_id
            for evidence_id in reason_evidence_ids
            if self._evidence_store.get(evidence_id) is None
        ]
        if missing:
            raise ValueError(
                "Approval references unknown evidence: " + ", ".join(missing)
            )
        approval = ApprovalRequest(
            approval_id=f"APPROVAL-{uuid4().hex}",
            action=action,
            target=target,
            target_version=target_version,
            risk_level=risk_level,
            requires_approval=risk_level
            in {ActionRiskLevel.HIGH, ActionRiskLevel.CRITICAL},
            impact_scope=impact_scope,
            reason_evidence_ids=reason_evidence_ids,
            requested_by=requested_by,
            requested_at=self._clock(),
        )
        self._approvals.create(approval)
        self._audit.record(
            entity_type="approval",
            entity_id=approval.approval_id,
            event_type="approval_requested",
            details={
                "action": approval.action,
                "target": approval.target,
                "target_version": approval.target_version,
                "risk_level": approval.risk_level.value,
                "evidence_ids": approval.reason_evidence_ids,
                "requested_by": approval.requested_by,
            },
        )
        return approval

    def decide(
        self,
        approval_id: str,
        *,
        approved: bool,
        decided_by: str,
    ) -> ApprovalDecisionResult:
        current = self._approvals.get(approval_id)
        if current is None:
            raise RecordNotFoundError(f"Approval not found: {approval_id}")
        if current.status is not ApprovalStatus.PENDING:
            raise ApprovalError("Approval request has already been decided")

        now = self._clock()
        token = self._token_factory() if approved else None
        updated = current.model_copy(
            update={
                "status": (
                    ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
                ),
                "decided_by": decided_by,
                "decided_at": now,
                "approval_token_hash": (
                    self._hash_token(token) if token is not None else None
                ),
                "token_expires_at": (
                    now + self._token_ttl if token is not None else None
                ),
            }
        )
        self._approvals.update(updated)
        self._audit.record(
            entity_type="approval",
            entity_id=approval_id,
            event_type="approval_decided",
            details={
                "status": updated.status.value,
                "decided_by": decided_by,
                "token_expires_at": (
                    updated.token_expires_at.isoformat()
                    if updated.token_expires_at is not None
                    else None
                ),
            },
        )
        return ApprovalDecisionResult(approval=updated, approval_token=token)

    def validate_and_consume(
        self,
        *,
        approval_id: str | None,
        approval_token: str | None,
        action: str,
        target: str,
        target_version: str | None,
    ) -> ApprovalRequest:
        if approval_id is None or approval_token is None:
            raise ApprovalError("A valid human approval token is required")
        approval = self._approvals.get(approval_id)
        if approval is None or approval.status is not ApprovalStatus.APPROVED:
            raise ApprovalError("Approval is missing or was not approved")
        if (
            approval.action != action
            or approval.target != target
            or approval.target_version != target_version
        ):
            raise ApprovalBindingError(
                "Approval token does not match action, target, and version"
            )
        if approval.token_used_at is not None:
            raise ApprovalTokenUsedError("Approval token has already been used")
        now = self._clock()
        if approval.token_expires_at is None or now >= approval.token_expires_at:
            raise ApprovalExpiredError("Approval token has expired")
        expected_hash = approval.approval_token_hash
        actual_hash = self._hash_token(approval_token)
        if expected_hash is None or not hmac.compare_digest(expected_hash, actual_hash):
            raise ApprovalError("Approval token is invalid")

        consumed = approval.model_copy(update={"token_used_at": now})
        self._approvals.update(consumed)
        self._audit.record(
            entity_type="approval",
            entity_id=approval.approval_id,
            event_type="approval_token_consumed",
            details={
                "action": action,
                "target": target,
                "target_version": target_version,
            },
        )
        return consumed
