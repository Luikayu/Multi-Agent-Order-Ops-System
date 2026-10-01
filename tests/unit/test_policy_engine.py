import pytest
from pydantic import ValidationError

from order_agent_ops.business.policy_engine import OrderPolicyEngine, PolicyDecision
from order_agent_ops.domain.enums import InventoryStatus, OrderStatus, RiskLevel
from order_agent_ops.domain.orders import InventoryCheckResult, RiskCheckResult


def inventory_result(status: InventoryStatus) -> InventoryCheckResult:
    return InventoryCheckResult(
        sku="SKU-001",
        requested_quantity=1,
        status=status,
        available_quantity=5 if status is InventoryStatus.SUFFICIENT else 0,
        reason=f"Inventory status is {status.value}",
    )


def risk_result(
    level: RiskLevel, *, requires_manual_review: bool = False
) -> RiskCheckResult:
    return RiskCheckResult(
        user_id="USER-001",
        risk_score=10,
        risk_level=level,
        reason=f"Risk level is {level.value}",
        requires_manual_review=requires_manual_review,
    )


@pytest.mark.parametrize(
    ("inventory_status", "risk_level", "expected"),
    [
        (inventory_status, risk_level, expected)
        for inventory_status, expected_by_risk in (
            (
                InventoryStatus.SUFFICIENT,
                {
                    RiskLevel.LOW: OrderStatus.APPROVED,
                    RiskLevel.MEDIUM: OrderStatus.APPROVED,
                    RiskLevel.HIGH: OrderStatus.MANUAL_REVIEW,
                    RiskLevel.UNKNOWN: OrderStatus.MANUAL_REVIEW,
                },
            ),
            (
                InventoryStatus.INSUFFICIENT,
                {
                    level: OrderStatus.REJECTED
                    for level in RiskLevel
                },
            ),
            (
                InventoryStatus.UNKNOWN,
                {
                    RiskLevel.LOW: OrderStatus.PENDING,
                    RiskLevel.MEDIUM: OrderStatus.PENDING,
                    RiskLevel.HIGH: OrderStatus.MANUAL_REVIEW,
                    RiskLevel.UNKNOWN: OrderStatus.MANUAL_REVIEW,
                },
            ),
            (
                InventoryStatus.TIMEOUT,
                {
                    RiskLevel.LOW: OrderStatus.PENDING,
                    RiskLevel.MEDIUM: OrderStatus.PENDING,
                    RiskLevel.HIGH: OrderStatus.MANUAL_REVIEW,
                    RiskLevel.UNKNOWN: OrderStatus.MANUAL_REVIEW,
                },
            ),
        )
        for risk_level, expected in expected_by_risk.items()
    ],
)
def test_complete_policy_matrix(
    inventory_status: InventoryStatus,
    risk_level: RiskLevel,
    expected: OrderStatus,
) -> None:
    decision = OrderPolicyEngine().decide(
        [inventory_result(inventory_status)], risk_result(risk_level)
    )

    assert decision.status is expected
    assert decision.allow_creation is (expected is OrderStatus.APPROVED)


def test_missing_inventory_is_pending() -> None:
    decision = OrderPolicyEngine().decide([], risk_result(RiskLevel.LOW))

    assert decision.status is OrderStatus.PENDING
    assert decision.allow_creation is False


def test_missing_risk_and_explicit_review_flag_require_manual_review() -> None:
    engine = OrderPolicyEngine()

    missing = engine.decide([inventory_result(InventoryStatus.SUFFICIENT)], None)
    flagged = engine.decide(
        [inventory_result(InventoryStatus.SUFFICIENT)],
        risk_result(RiskLevel.LOW, requires_manual_review=True),
    )

    assert missing.status is OrderStatus.MANUAL_REVIEW
    assert flagged.status is OrderStatus.MANUAL_REVIEW


def test_policy_decision_rejects_inconsistent_creation_permission() -> None:
    with pytest.raises(ValidationError):
        PolicyDecision(
            status=OrderStatus.PENDING,
            allow_creation=True,
            reason="Unsafe mismatch",
        )
