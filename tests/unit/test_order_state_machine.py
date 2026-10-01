import pytest

from order_agent_ops.business.order_state_machine import (
    InvalidOrderTransitionError,
    OrderStateMachine,
)
from order_agent_ops.domain.enums import OrderStatus


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (OrderStatus.RECEIVED, OrderStatus.CHECKING),
        (OrderStatus.CHECKING, OrderStatus.APPROVED),
        (OrderStatus.CHECKING, OrderStatus.PENDING),
        (OrderStatus.CHECKING, OrderStatus.MANUAL_REVIEW),
        (OrderStatus.CHECKING, OrderStatus.REJECTED),
        (OrderStatus.APPROVED, OrderStatus.CREATING),
        (OrderStatus.PENDING, OrderStatus.REPLAYING),
        (OrderStatus.REPLAYING, OrderStatus.CHECKING),
        (OrderStatus.CREATING, OrderStatus.COMPLETED),
        (OrderStatus.CREATING, OrderStatus.FAILED),
        (OrderStatus.MANUAL_REVIEW, OrderStatus.APPROVED),
        (OrderStatus.MANUAL_REVIEW, OrderStatus.REJECTED),
    ],
)
def test_documented_order_transitions_are_allowed(
    current: OrderStatus, target: OrderStatus
) -> None:
    assert OrderStateMachine.transition(current, target) is target


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (OrderStatus.RECEIVED, OrderStatus.COMPLETED),
        (OrderStatus.CHECKING, OrderStatus.CREATING),
        (OrderStatus.COMPLETED, OrderStatus.CHECKING),
        (OrderStatus.REJECTED, OrderStatus.RECEIVED),
        (OrderStatus.FAILED, OrderStatus.CREATING),
    ],
)
def test_invalid_order_transitions_raise_business_error(
    current: OrderStatus, target: OrderStatus
) -> None:
    with pytest.raises(
        InvalidOrderTransitionError,
        match=f"{current.value} -> {target.value}",
    ):
        OrderStateMachine.transition(current, target)
