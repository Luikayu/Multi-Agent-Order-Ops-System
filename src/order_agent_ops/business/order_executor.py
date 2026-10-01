"""Atomic and idempotent execution of approved order decisions."""

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal

from order_agent_ops.business.policy_engine import PolicyDecision
from order_agent_ops.domain.base import DomainModel, OrderId, RequestId, TraceId
from order_agent_ops.domain.enums import OrderStatus
from order_agent_ops.domain.orders import OrderItem, OrderRecord, OrderRequest
from order_agent_ops.storage.database import Database
from order_agent_ops.storage.repositories import InventoryRepository, OrderRepository
from order_agent_ops.storage.repositories import RecordNotFoundError


class OrderExecutionError(RuntimeError):
    """Base error for deterministic order execution."""


class OrderExecutionNotAllowedError(OrderExecutionError):
    """Raised when a non-approved policy decision reaches the executor."""


class IdempotencyConflictError(OrderExecutionError):
    """Raised when one idempotency key is reused for different business input."""


class OrderExecutionResult(DomainModel):
    order: OrderRecord
    idempotent_replay: bool
    reserved_quantities: dict[str, int]


class OrderExecutorService:
    """Reserve inventory and create one order in a single SQLite transaction."""

    def __init__(
        self,
        database: Database,
        order_repository: OrderRepository,
        inventory_repository: InventoryRepository,
    ) -> None:
        self._database = database
        self._orders = order_repository
        self._inventory = inventory_repository

    def execute(
        self,
        order_request: OrderRequest,
        decision: PolicyDecision,
        *,
        request_id: RequestId,
        order_id: OrderId,
        trace_id: TraceId,
    ) -> OrderExecutionResult:
        if decision.status is not OrderStatus.APPROVED or not decision.allow_creation:
            raise OrderExecutionNotAllowedError(
                f"Order creation is not allowed for {decision.status.value}"
            )

        with self._database.transaction(immediate=True) as connection:
            existing = self._orders.get_by_idempotency_key_with_connection(
                connection, order_request.idempotency_key
            )
            if existing is not None:
                if not self.matches_request(existing, order_request):
                    raise IdempotencyConflictError(
                        "Idempotency key was already used for different order input"
                    )
                return OrderExecutionResult(
                    order=existing,
                    idempotent_replay=True,
                    reserved_quantities={},
                )

            reserved_quantities = self._aggregate_quantities(order_request.items)
            for sku in sorted(reserved_quantities):
                self._inventory.reserve_with_connection(
                    connection, sku, reserved_quantities[sku]
                )

            now = datetime.now(timezone.utc)
            order = OrderRecord(
                request_id=request_id,
                order_id=order_id,
                trace_id=trace_id,
                user_id=order_request.user_id,
                items=order_request.items,
                total_amount=order_request.total_amount,
                idempotency_key=order_request.idempotency_key,
                status=OrderStatus.RECEIVED,
                created_at=now,
                updated_at=now,
            )
            self._orders.create_with_connection(connection, order)
            for target, reason in (
                (OrderStatus.CHECKING, "Execution inputs accepted"),
                (OrderStatus.APPROVED, decision.reason),
                (OrderStatus.CREATING, "Inventory reserved atomically"),
                (OrderStatus.COMPLETED, "Order persisted successfully"),
            ):
                order = self._orders.transition_status_with_connection(
                    connection,
                    order.order_id,
                    target,
                    trigger_component="order-executor",
                    reason=reason,
                )

            return OrderExecutionResult(
                order=order,
                idempotent_replay=False,
                reserved_quantities=dict(reserved_quantities),
            )

    def execute_replay(
        self,
        order_id: OrderId,
        decision: PolicyDecision,
        *,
        replay_trace_id: TraceId,
        references: list[str] | None = None,
    ) -> OrderExecutionResult:
        """Complete an existing CHECKING order without creating a second record."""

        if decision.status is not OrderStatus.APPROVED or not decision.allow_creation:
            raise OrderExecutionNotAllowedError(
                f"Order replay is not allowed for {decision.status.value}"
            )
        with self._database.transaction(immediate=True) as connection:
            current = self._orders.get_with_connection(connection, order_id)
            if current is None:
                raise RecordNotFoundError(f"Order not found: {order_id}")
            if current.status is not OrderStatus.CHECKING:
                raise OrderExecutionNotAllowedError(
                    "Replayed order must be in CHECKING status"
                )

            reserved_quantities = self._aggregate_quantities(current.items)
            for sku in sorted(reserved_quantities):
                self._inventory.reserve_with_connection(
                    connection, sku, reserved_quantities[sku]
                )

            replay_references = list(
                dict.fromkeys([*(references or []), replay_trace_id])
            )
            order = current
            for target, reason in (
                (OrderStatus.APPROVED, decision.reason),
                (OrderStatus.CREATING, "Replay inventory reserved atomically"),
                (OrderStatus.COMPLETED, "Pending order replay completed"),
            ):
                order = self._orders.transition_status_with_connection(
                    connection,
                    order.order_id,
                    target,
                    trigger_component="replay-service",
                    reason=reason,
                    references=replay_references,
                )
            return OrderExecutionResult(
                order=order,
                idempotent_replay=False,
                reserved_quantities=dict(reserved_quantities),
            )

    @staticmethod
    def _aggregate_quantities(items: list[OrderItem]) -> dict[str, int]:
        quantities: defaultdict[str, int] = defaultdict(int)
        for item in items:
            quantities[item.sku] += item.quantity
        return dict(quantities)

    @staticmethod
    def matches_request(existing: OrderRecord, request: OrderRequest) -> bool:
        """Check whether a retry contains the same immutable business input."""

        return (
            existing.user_id == request.user_id
            and existing.items == request.items
            and Decimal(existing.total_amount) == Decimal(request.total_amount)
            and existing.idempotency_key == request.idempotency_key
        )
