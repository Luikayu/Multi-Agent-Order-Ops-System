import sqlite3
from decimal import Decimal

import pytest

from order_agent_ops.business.order_executor import (
    IdempotencyConflictError,
    OrderExecutionNotAllowedError,
    OrderExecutorService,
)
from order_agent_ops.business.policy_engine import PolicyDecision
from order_agent_ops.domain.enums import OrderStatus
from order_agent_ops.domain.orders import OrderItem, OrderRecord, OrderRequest
from order_agent_ops.storage.database import Database
from order_agent_ops.storage.repositories import (
    AuditRepository,
    InsufficientInventoryError,
    InventoryRepository,
    OrderRepository,
    StateTransitionRequiredError,
)


@pytest.fixture
def database(tmp_path) -> Database:
    result = Database(tmp_path / "state" / "executor.sqlite3")
    result.initialize()
    return result


def request(*, quantity: int = 2, key: str = "idem-001") -> OrderRequest:
    return OrderRequest(
        user_id="USER-001",
        items=[
            OrderItem(
                sku="SKU-001", quantity=quantity, unit_price=Decimal("10.00")
            )
        ],
        total_amount=Decimal("10.00") * quantity,
        idempotency_key=key,
    )


def approved() -> PolicyDecision:
    return PolicyDecision(
        status=OrderStatus.APPROVED,
        allow_creation=True,
        reason="Inventory and risk checks passed",
    )


def executor(
    database: Database, order_repository: OrderRepository | None = None
) -> OrderExecutorService:
    return OrderExecutorService(
        database,
        order_repository or OrderRepository(database),
        InventoryRepository(database),
    )


def execute(
    service: OrderExecutorService,
    order_request: OrderRequest,
    *,
    order_id: str = "ORD-001",
):
    return service.execute(
        order_request,
        approved(),
        request_id="REQ-001",
        order_id=order_id,
        trace_id="TRACE-001",
    )


def test_approved_execution_reserves_inventory_and_completes_order(
    database: Database,
) -> None:
    inventory = InventoryRepository(database)
    inventory.set_quantity("SKU-001", 5)

    result = execute(executor(database), request())

    assert result.order.status is OrderStatus.COMPLETED
    assert result.idempotent_replay is False
    assert result.reserved_quantities == {"SKU-001": 2}
    assert inventory.get("SKU-001").quantity == 3
    transitions = AuditRepository(database).list_for_entity("order", "ORD-001")
    assert [event.details["to_status"] for event in transitions] == [
        "CHECKING",
        "APPROVED",
        "CREATING",
        "COMPLETED",
    ]


def test_same_idempotency_key_returns_original_without_second_deduction(
    database: Database,
) -> None:
    inventory = InventoryRepository(database)
    inventory.set_quantity("SKU-001", 5)
    service = executor(database)
    first = execute(service, request(), order_id="ORD-001")

    second = execute(service, request(), order_id="ORD-RETRY")

    assert second.idempotent_replay is True
    assert second.order.order_id == first.order.order_id == "ORD-001"
    assert second.reserved_quantities == {}
    assert inventory.get("SKU-001").quantity == 3
    assert [order.order_id for order in OrderRepository(database).list_all()] == [
        "ORD-001"
    ]


def test_reusing_idempotency_key_for_different_input_is_rejected(
    database: Database,
) -> None:
    inventory = InventoryRepository(database)
    inventory.set_quantity("SKU-001", 8)
    service = executor(database)
    execute(service, request(quantity=2))

    with pytest.raises(IdempotencyConflictError):
        execute(service, request(quantity=3), order_id="ORD-002")

    assert inventory.get("SKU-001").quantity == 6
    assert len(OrderRepository(database).list_all()) == 1


def test_actual_inventory_shortage_rolls_back_the_whole_execution(
    database: Database,
) -> None:
    inventory = InventoryRepository(database)
    inventory.set_quantity("SKU-001", 1)

    with pytest.raises(InsufficientInventoryError):
        execute(executor(database), request(quantity=2))

    assert inventory.get("SKU-001").quantity == 1
    assert OrderRepository(database).list_all() == []


class FailingOrderRepository(OrderRepository):
    def create_with_connection(
        self, connection: sqlite3.Connection, record: OrderRecord
    ) -> None:
        raise RuntimeError("simulated order write failure")


def test_order_write_failure_rolls_back_inventory_reservation(
    database: Database,
) -> None:
    inventory = InventoryRepository(database)
    inventory.set_quantity("SKU-001", 5)
    failing_orders = FailingOrderRepository(database)

    with pytest.raises(RuntimeError, match="simulated order write failure"):
        execute(executor(database, failing_orders), request())

    assert inventory.get("SKU-001").quantity == 5
    assert OrderRepository(database).list_all() == []
    inventory_events = AuditRepository(database).list_for_entity(
        "inventory", "SKU-001"
    )
    assert [event.event_type for event in inventory_events] == ["quantity_set"]


def test_non_approved_decision_cannot_create_or_reserve(database: Database) -> None:
    inventory = InventoryRepository(database)
    inventory.set_quantity("SKU-001", 5)
    decision = PolicyDecision(
        status=OrderStatus.PENDING,
        allow_creation=False,
        reason="Inventory timed out",
    )

    with pytest.raises(OrderExecutionNotAllowedError):
        executor(database).execute(
            request(),
            decision,
            request_id="REQ-001",
            order_id="ORD-001",
            trace_id="TRACE-001",
        )

    assert inventory.get("SKU-001").quantity == 5
    assert OrderRepository(database).list_all() == []


def test_repository_cannot_create_an_order_directly_in_a_final_state(
    database: Database,
) -> None:
    completed = OrderRecord(
        request_id="REQ-001",
        order_id="ORD-001",
        trace_id="TRACE-001",
        user_id="USER-001",
        items=request().items,
        total_amount=Decimal("20.00"),
        idempotency_key="idem-001",
        status=OrderStatus.COMPLETED,
        created_at="2026-09-27T12:00:00Z",
        updated_at="2026-09-27T12:00:00Z",
    )

    with pytest.raises(StateTransitionRequiredError):
        OrderRepository(database).create(completed)

    assert OrderRepository(database).list_all() == []
