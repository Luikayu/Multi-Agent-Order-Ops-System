"""Deterministic conversion of Agent check results into an order decision."""

from typing import Literal

from pydantic import model_validator

from order_agent_ops.domain.base import DomainModel, NonEmptyString
from order_agent_ops.domain.enums import InventoryStatus, OrderStatus, RiskLevel
from order_agent_ops.domain.orders import InventoryCheckResult, RiskCheckResult


DecisionStatus = Literal[
    OrderStatus.APPROVED,
    OrderStatus.REJECTED,
    OrderStatus.PENDING,
    OrderStatus.MANUAL_REVIEW,
]


class PolicyDecision(DomainModel):
    """A validated decision that cannot mark an unsafe state as creatable."""

    status: DecisionStatus
    allow_creation: bool
    reason: NonEmptyString

    @model_validator(mode="after")
    def creation_requires_approval(self) -> "PolicyDecision":
        should_allow = self.status is OrderStatus.APPROVED
        if self.allow_creation is not should_allow:
            raise ValueError("allow_creation must be true only for APPROVED decisions")
        return self


class OrderPolicyEngine:
    """Apply the stage-8 safety matrix without calling any model."""

    def decide(
        self,
        inventory_results: list[InventoryCheckResult],
        risk_result: RiskCheckResult | None,
    ) -> PolicyDecision:
        # Confirmed inability to fulfil wins over all other signals.
        if any(
            result.status is InventoryStatus.INSUFFICIENT
            for result in inventory_results
        ):
            return PolicyDecision(
                status=OrderStatus.REJECTED,
                allow_creation=False,
                reason="At least one item has insufficient inventory",
            )

        # Unknown or explicitly reviewable risk must never be auto-approved.
        if (
            risk_result is None
            or risk_result.risk_level in {RiskLevel.HIGH, RiskLevel.UNKNOWN}
            or risk_result.requires_manual_review
        ):
            return PolicyDecision(
                status=OrderStatus.MANUAL_REVIEW,
                allow_creation=False,
                reason="Risk result requires manual review",
            )

        # Missing checks, adapter uncertainty and timeouts are retryable states.
        if not inventory_results or any(
            result.status in {InventoryStatus.UNKNOWN, InventoryStatus.TIMEOUT}
            for result in inventory_results
        ):
            return PolicyDecision(
                status=OrderStatus.PENDING,
                allow_creation=False,
                reason="Inventory result is incomplete, unknown, or timed out",
            )

        if all(
            result.status is InventoryStatus.SUFFICIENT
            for result in inventory_results
        ) and risk_result.risk_level in {RiskLevel.LOW, RiskLevel.MEDIUM}:
            return PolicyDecision(
                status=OrderStatus.APPROVED,
                allow_creation=True,
                reason="Inventory is sufficient and risk checks passed",
            )

        # Defensive fallback for future enum extensions.
        return PolicyDecision(
            status=OrderStatus.PENDING,
            allow_creation=False,
            reason="Policy inputs did not match a safe creation rule",
        )
