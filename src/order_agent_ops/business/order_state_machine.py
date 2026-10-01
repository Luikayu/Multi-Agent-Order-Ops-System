"""Deterministic order state transitions."""

from order_agent_ops.domain.enums import OrderStatus


class InvalidOrderTransitionError(ValueError):
    """Raised when an order attempts a transition outside the state graph."""


class OrderStateMachine:
    """Validate the order lifecycle defined in README.md."""

    _transitions: dict[OrderStatus, frozenset[OrderStatus]] = {
        OrderStatus.RECEIVED: frozenset({OrderStatus.CHECKING}),
        OrderStatus.CHECKING: frozenset(
            {
                OrderStatus.APPROVED,
                OrderStatus.PENDING,
                OrderStatus.MANUAL_REVIEW,
                OrderStatus.REJECTED,
            }
        ),
        OrderStatus.APPROVED: frozenset({OrderStatus.CREATING}),
        OrderStatus.PENDING: frozenset({OrderStatus.REPLAYING}),
        OrderStatus.REPLAYING: frozenset({OrderStatus.CHECKING}),
        OrderStatus.CREATING: frozenset(
            {OrderStatus.COMPLETED, OrderStatus.FAILED}
        ),
        OrderStatus.MANUAL_REVIEW: frozenset(
            {OrderStatus.APPROVED, OrderStatus.REJECTED}
        ),
        OrderStatus.COMPLETED: frozenset(),
        OrderStatus.FAILED: frozenset(),
        OrderStatus.REJECTED: frozenset(),
    }

    @classmethod
    def allowed_transitions(cls, current: OrderStatus) -> frozenset[OrderStatus]:
        return cls._transitions[current]

    @classmethod
    def transition(cls, current: OrderStatus, target: OrderStatus) -> OrderStatus:
        if target not in cls.allowed_transitions(current):
            raise InvalidOrderTransitionError(
                f"Invalid order state transition: {current.value} -> {target.value}"
            )
        return target
