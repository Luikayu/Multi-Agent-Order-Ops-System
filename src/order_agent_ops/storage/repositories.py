"""Repository layer for SQLite-backed domain records."""

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, ClassVar, Generic, TypeVar, cast
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, Field

from order_agent_ops.business.order_state_machine import OrderStateMachine
from order_agent_ops.domain.agents import AgentRunRecord, ToolCallRecord
from order_agent_ops.domain.approvals import ApprovalRequest, RemediationAction
from order_agent_ops.domain.base import DomainModel, NonEmptyString
from order_agent_ops.domain.enums import IncidentStatus, OrderStatus
from order_agent_ops.domain.evidence import EvidenceRecord
from order_agent_ops.domain.incidents import IncidentRecord
from order_agent_ops.domain.orders import OrderRecord
from order_agent_ops.domain.shopping import PurchaseIntentRecord
from order_agent_ops.ops.incident_state_machine import IncidentStateMachine
from order_agent_ops.storage.database import Database


ModelT = TypeVar("ModelT", bound=BaseModel)
SQL_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")


class RepositoryError(RuntimeError):
    """Base persistence error exposed to services."""


class DuplicateRecordError(RepositoryError):
    """Raised when a repository identifier already exists."""


class RecordNotFoundError(RepositoryError):
    """Raised when an update or transition targets a missing record."""


class StateTransitionRequiredError(RepositoryError):
    """Raised when code tries to bypass a repository state machine."""


class InventoryUnavailableError(RepositoryError):
    """Raised when a reservation targets an unknown inventory item."""


class InsufficientInventoryError(RepositoryError):
    """Raised when an atomic reservation cannot satisfy the requested quantity."""


class InventoryRecord(DomainModel):
    sku: NonEmptyString
    quantity: int = Field(ge=0)
    created_at: AwareDatetime
    updated_at: AwareDatetime


class AuditEventRecord(DomainModel):
    event_id: NonEmptyString
    entity_type: NonEmptyString
    entity_id: NonEmptyString
    event_type: NonEmptyString
    occurred_at: AwareDatetime
    details: dict[str, Any]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _write_audit_event(
    connection: sqlite3.Connection,
    *,
    entity_type: str,
    entity_id: str,
    event_type: str,
    occurred_at: datetime,
    details: dict[str, Any],
) -> AuditEventRecord:
    event = AuditEventRecord(
        event_id=f"AUDIT-{uuid4().hex}",
        entity_type=entity_type,
        entity_id=entity_id,
        event_type=event_type,
        occurred_at=occurred_at,
        details=details,
    )
    connection.execute(
        """
        INSERT INTO audit_events (
            event_id, entity_type, entity_id, event_type, occurred_at, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            event.event_id,
            event.entity_type,
            event.entity_id,
            event.event_type,
            event.occurred_at.isoformat(),
            json.dumps(event.details, ensure_ascii=False, separators=(",", ":")),
        ),
    )
    return event


class JsonModelRepository(Generic[ModelT]):
    """Shared CRUD implementation for strict Pydantic records."""

    table_name: ClassVar[str]
    id_field: ClassVar[str]
    entity_type: ClassVar[str]
    model_type: ClassVar[type[BaseModel]]

    def __init__(self, database: Database) -> None:
        self.database = database
        if not SQL_IDENTIFIER.fullmatch(self.table_name):
            raise ValueError(f"Unsafe repository table name: {self.table_name}")
        if not SQL_IDENTIFIER.fullmatch(self.id_field):
            raise ValueError(f"Unsafe repository identifier field: {self.id_field}")

    def _identifier(self, record: ModelT) -> str:
        return str(getattr(record, self.id_field))

    def _deserialize(self, row: sqlite3.Row) -> ModelT:
        return cast(ModelT, self.model_type.model_validate_json(row["payload_json"]))

    def _get_with_connection(
        self, connection: sqlite3.Connection, record_id: str
    ) -> ModelT | None:
        row = connection.execute(
            f"SELECT payload_json FROM {self.table_name} WHERE {self.id_field} = ?",
            (record_id,),
        ).fetchone()
        return None if row is None else self._deserialize(row)

    def create(self, record: ModelT) -> ModelT:
        timestamp = utc_now().isoformat()
        try:
            with self.database.connection() as connection:
                connection.execute(
                    f"""
                    INSERT INTO {self.table_name} (
                        {self.id_field}, payload_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        self._identifier(record),
                        record.model_dump_json(),
                        timestamp,
                        timestamp,
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise DuplicateRecordError(
                f"{self.table_name} record already exists: {self._identifier(record)}"
            ) from error
        return record

    def get(self, record_id: str) -> ModelT | None:
        with self.database.connection() as connection:
            return self._get_with_connection(connection, record_id)

    def list_all(self) -> list[ModelT]:
        with self.database.connection() as connection:
            rows = connection.execute(
                f"SELECT payload_json FROM {self.table_name} ORDER BY created_at"
            ).fetchall()
        return [self._deserialize(row) for row in rows]

    def _update_with_connection(
        self, connection: sqlite3.Connection, record: ModelT
    ) -> None:
        result = connection.execute(
            f"""
            UPDATE {self.table_name}
            SET payload_json = ?, updated_at = ?
            WHERE {self.id_field} = ?
            """,
            (
                record.model_dump_json(),
                utc_now().isoformat(),
                self._identifier(record),
            ),
        )
        if result.rowcount != 1:
            raise RecordNotFoundError(
                f"{self.table_name} record not found: {self._identifier(record)}"
            )

    def update(self, record: ModelT) -> ModelT:
        with self.database.connection() as connection:
            self._update_with_connection(connection, record)
        return record

    def delete(self, record_id: str) -> None:
        with self.database.connection() as connection:
            result = connection.execute(
                f"DELETE FROM {self.table_name} WHERE {self.id_field} = ?",
                (record_id,),
            )
            if result.rowcount != 1:
                raise RecordNotFoundError(
                    f"{self.table_name} record not found: {record_id}"
                )
            _write_audit_event(
                connection,
                entity_type=self.entity_type,
                entity_id=record_id,
                event_type="deleted",
                occurred_at=utc_now(),
                details={"reason": "repository delete"},
            )


class OrderRepository(JsonModelRepository[OrderRecord]):
    table_name = "orders"
    id_field = "order_id"
    entity_type = "order"
    model_type = OrderRecord

    def get_with_connection(
        self, connection: sqlite3.Connection, order_id: str
    ) -> OrderRecord | None:
        return self._get_with_connection(connection, order_id)

    def create(self, record: OrderRecord) -> OrderRecord:
        try:
            with self.database.connection() as connection:
                self.create_with_connection(connection, record)
        except sqlite3.IntegrityError as error:
            raise DuplicateRecordError(
                "Order identifier or idempotency key already exists: "
                f"{record.order_id} / {record.idempotency_key}"
            ) from error
        return record

    def create_with_connection(
        self, connection: sqlite3.Connection, record: OrderRecord
    ) -> None:
        if record.status is not OrderStatus.RECEIVED:
            raise StateTransitionRequiredError(
                "New orders must start in RECEIVED; final states require transitions"
            )
        timestamp = utc_now().isoformat()
        connection.execute(
            """
            INSERT INTO orders (
                order_id, idempotency_key, payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                record.order_id,
                record.idempotency_key,
                record.model_dump_json(),
                timestamp,
                timestamp,
            ),
        )

    def get_by_idempotency_key(self, idempotency_key: str) -> OrderRecord | None:
        with self.database.connection() as connection:
            return self.get_by_idempotency_key_with_connection(
                connection, idempotency_key
            )

    def get_by_idempotency_key_with_connection(
        self, connection: sqlite3.Connection, idempotency_key: str
    ) -> OrderRecord | None:
        row = connection.execute(
            "SELECT payload_json FROM orders WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        return None if row is None else self._deserialize(row)

    def _update_with_connection(
        self, connection: sqlite3.Connection, record: OrderRecord
    ) -> None:
        result = connection.execute(
            """
            UPDATE orders
            SET idempotency_key = ?, payload_json = ?, updated_at = ?
            WHERE order_id = ?
            """,
            (
                record.idempotency_key,
                record.model_dump_json(),
                utc_now().isoformat(),
                record.order_id,
            ),
        )
        if result.rowcount != 1:
            raise RecordNotFoundError(f"Order not found: {record.order_id}")

    def update(self, record: OrderRecord) -> OrderRecord:
        with self.database.connection() as connection:
            current = self._get_with_connection(connection, record.order_id)
            if current is None:
                raise RecordNotFoundError(f"Order not found: {record.order_id}")
            if current.status != record.status:
                raise StateTransitionRequiredError(
                    "Order status changes must use transition_status"
                )
            if current.idempotency_key != record.idempotency_key:
                raise RepositoryError("Order idempotency_key is immutable")
            self._update_with_connection(connection, record)
        return record

    def transition_status(
        self,
        order_id: str,
        target: OrderStatus,
        *,
        trigger_component: str,
        reason: str,
        references: list[str] | None = None,
    ) -> OrderRecord:
        with self.database.connection() as connection:
            return self.transition_status_with_connection(
                connection,
                order_id,
                target,
                trigger_component=trigger_component,
                reason=reason,
                references=references,
            )

    def transition_status_with_connection(
        self,
        connection: sqlite3.Connection,
        order_id: str,
        target: OrderStatus,
        *,
        trigger_component: str,
        reason: str,
        references: list[str] | None = None,
    ) -> OrderRecord:
        transition_time = utc_now()
        current = self._get_with_connection(connection, order_id)
        if current is None:
            raise RecordNotFoundError(f"Order not found: {order_id}")

        OrderStateMachine.transition(current.status, target)
        updated = current.model_copy(
            update={"status": target, "updated_at": transition_time}
        )
        self._update_with_connection(connection, updated)
        _write_audit_event(
            connection,
            entity_type="order",
            entity_id=order_id,
            event_type="state_transition",
            occurred_at=transition_time,
            details={
                "request_id": current.request_id,
                "order_id": current.order_id,
                "trace_id": current.trace_id,
                "from_status": current.status.value,
                "to_status": target.value,
                "trigger_component": trigger_component,
                "reason": reason,
                "references": references or [],
            },
        )
        return updated


class AgentRunRepository(JsonModelRepository[AgentRunRecord]):
    table_name = "agent_runs"
    id_field = "run_id"
    entity_type = "agent_run"
    model_type = AgentRunRecord


class ToolCallRepository(JsonModelRepository[ToolCallRecord]):
    table_name = "tool_calls"
    id_field = "call_id"
    entity_type = "tool_call"
    model_type = ToolCallRecord


class PurchaseIntentRepository(JsonModelRepository[PurchaseIntentRecord]):
    table_name = "purchase_intents"
    id_field = "intent_id"
    entity_type = "purchase_intent"
    model_type = PurchaseIntentRecord


class IncidentRepository(JsonModelRepository[IncidentRecord]):
    table_name = "incidents"
    id_field = "incident_id"
    entity_type = "incident"
    model_type = IncidentRecord

    def update(self, record: IncidentRecord) -> IncidentRecord:
        with self.database.connection() as connection:
            current = self._get_with_connection(connection, record.incident_id)
            if current is None:
                raise RecordNotFoundError(f"Incident not found: {record.incident_id}")
            if current.status != record.status:
                raise StateTransitionRequiredError(
                    "Incident status changes must use transition_status"
                )
            self._update_with_connection(connection, record)
        return record

    def transition_status(
        self,
        incident_id: str,
        target: IncidentStatus,
        *,
        trigger_component: str,
        reason: str,
        references: list[str] | None = None,
    ) -> IncidentRecord:
        transition_time = utc_now()
        with self.database.connection() as connection:
            current = self._get_with_connection(connection, incident_id)
            if current is None:
                raise RecordNotFoundError(f"Incident not found: {incident_id}")

            IncidentStateMachine.transition(current.status, target)
            updated = current.model_copy(update={"status": target})
            self._update_with_connection(connection, updated)
            _write_audit_event(
                connection,
                entity_type="incident",
                entity_id=incident_id,
                event_type="state_transition",
                occurred_at=transition_time,
                details={
                    "incident_id": current.incident_id,
                    "object_id": current.object_id,
                    "from_status": current.status.value,
                    "to_status": target.value,
                    "trigger_component": trigger_component,
                    "reason": reason,
                    "references": references or [],
                },
            )
        return updated


class EvidenceRepository(JsonModelRepository[EvidenceRecord]):
    table_name = "evidence"
    id_field = "evidence_id"
    entity_type = "evidence"
    model_type = EvidenceRecord


class ApprovalRepository(JsonModelRepository[ApprovalRequest]):
    table_name = "approvals"
    id_field = "approval_id"
    entity_type = "approval"
    model_type = ApprovalRequest


class RemediationActionRepository(JsonModelRepository[RemediationAction]):
    table_name = "remediation_actions"
    id_field = "action_id"
    entity_type = "remediation_action"
    model_type = RemediationAction

    def get_by_idempotency_key(
        self, idempotency_key: str
    ) -> RemediationAction | None:
        return next(
            (
                action
                for action in self.list_all()
                if action.idempotency_key == idempotency_key
            ),
            None,
        )


class InventoryRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def set_quantity(self, sku: str, quantity: int) -> InventoryRecord:
        if not sku.strip():
            raise ValueError("SKU must not be empty")
        if quantity < 0:
            raise ValueError("Inventory quantity must not be negative")

        timestamp = utc_now()
        with self.database.connection() as connection:
            connection.execute(
                """
                INSERT INTO inventory (sku, quantity, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(sku) DO UPDATE SET
                    quantity = excluded.quantity,
                    updated_at = excluded.updated_at
                """,
                (sku, quantity, timestamp.isoformat(), timestamp.isoformat()),
            )
            _write_audit_event(
                connection,
                entity_type="inventory",
                entity_id=sku,
                event_type="quantity_set",
                occurred_at=timestamp,
                details={"quantity": quantity},
            )
        record = self.get(sku)
        if record is None:  # pragma: no cover - the transaction above guarantees this
            raise RepositoryError(f"Inventory write could not be read back: {sku}")
        return record

    @staticmethod
    def _record_from_row(row: sqlite3.Row | None) -> InventoryRecord | None:
        return None if row is None else InventoryRecord.model_validate(dict(row))

    def get_with_connection(
        self, connection: sqlite3.Connection, sku: str
    ) -> InventoryRecord | None:
        row = connection.execute(
            """
            SELECT sku, quantity, created_at, updated_at
            FROM inventory WHERE sku = ?
            """,
            (sku,),
        ).fetchone()
        return self._record_from_row(row)

    def get(self, sku: str) -> InventoryRecord | None:
        with self.database.connection() as connection:
            return self.get_with_connection(connection, sku)

    def reserve(self, sku: str, quantity: int) -> InventoryRecord:
        with self.database.transaction(immediate=True) as connection:
            return self.reserve_with_connection(connection, sku, quantity)

    def reserve_with_connection(
        self, connection: sqlite3.Connection, sku: str, quantity: int
    ) -> InventoryRecord:
        if quantity <= 0:
            raise ValueError("Reservation quantity must be positive")
        current = self.get_with_connection(connection, sku)
        if current is None:
            raise InventoryUnavailableError(f"Inventory record not found: {sku}")
        if current.quantity < quantity:
            raise InsufficientInventoryError(
                f"Insufficient inventory for {sku}: "
                f"requested {quantity}, available {current.quantity}"
            )

        timestamp = utc_now()
        remaining = current.quantity - quantity
        connection.execute(
            "UPDATE inventory SET quantity = ?, updated_at = ? WHERE sku = ?",
            (remaining, timestamp.isoformat(), sku),
        )
        _write_audit_event(
            connection,
            entity_type="inventory",
            entity_id=sku,
            event_type="reserved",
            occurred_at=timestamp,
            details={
                "quantity": quantity,
                "before_quantity": current.quantity,
                "after_quantity": remaining,
            },
        )
        updated = self.get_with_connection(connection, sku)
        if updated is None:  # pragma: no cover - the update above guarantees this
            raise RepositoryError(f"Inventory reservation could not be read back: {sku}")
        return updated

    def release(self, sku: str, quantity: int) -> InventoryRecord:
        with self.database.transaction(immediate=True) as connection:
            return self.release_with_connection(connection, sku, quantity)

    def release_with_connection(
        self, connection: sqlite3.Connection, sku: str, quantity: int
    ) -> InventoryRecord:
        if quantity <= 0:
            raise ValueError("Release quantity must be positive")
        current = self.get_with_connection(connection, sku)
        if current is None:
            raise InventoryUnavailableError(f"Inventory record not found: {sku}")

        timestamp = utc_now()
        restored = current.quantity + quantity
        connection.execute(
            "UPDATE inventory SET quantity = ?, updated_at = ? WHERE sku = ?",
            (restored, timestamp.isoformat(), sku),
        )
        _write_audit_event(
            connection,
            entity_type="inventory",
            entity_id=sku,
            event_type="released",
            occurred_at=timestamp,
            details={
                "quantity": quantity,
                "before_quantity": current.quantity,
                "after_quantity": restored,
            },
        )
        updated = self.get_with_connection(connection, sku)
        if updated is None:  # pragma: no cover - the update above guarantees this
            raise RepositoryError(f"Inventory release could not be read back: {sku}")
        return updated

    def list_all(self) -> list[InventoryRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT sku, quantity, created_at, updated_at
                FROM inventory ORDER BY sku
                """
            ).fetchall()
        return [InventoryRecord.model_validate(dict(row)) for row in rows]

    def delete(self, sku: str) -> None:
        with self.database.connection() as connection:
            result = connection.execute("DELETE FROM inventory WHERE sku = ?", (sku,))
            if result.rowcount != 1:
                raise RecordNotFoundError(f"Inventory record not found: {sku}")
            _write_audit_event(
                connection,
                entity_type="inventory",
                entity_id=sku,
                event_type="deleted",
                occurred_at=utc_now(),
                details={"reason": "repository delete"},
            )


class AuditRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def list_for_entity(
        self, entity_type: str, entity_id: str
    ) -> list[AuditEventRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT event_id, entity_type, entity_id, event_type,
                       occurred_at, payload_json
                FROM audit_events
                WHERE entity_type = ? AND entity_id = ?
                ORDER BY occurred_at, rowid
                """,
                (entity_type, entity_id),
            ).fetchall()
        return [
            AuditEventRecord(
                event_id=row["event_id"],
                entity_type=row["entity_type"],
                entity_id=row["entity_id"],
                event_type=row["event_type"],
                occurred_at=row["occurred_at"],
                details=json.loads(row["payload_json"]),
            )
            for row in rows
        ]

    def record(
        self,
        *,
        entity_type: str,
        entity_id: str,
        event_type: str,
        details: dict[str, Any],
    ) -> AuditEventRecord:
        with self.database.connection() as connection:
            return _write_audit_event(
                connection,
                entity_type=entity_type,
                entity_id=entity_id,
                event_type=event_type,
                occurred_at=utc_now(),
                details=details,
            )
