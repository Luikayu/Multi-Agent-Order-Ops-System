"""Order, inventory, and risk contracts."""

from decimal import Decimal

from pydantic import AwareDatetime, Field

from order_agent_ops.domain.base import (
    DomainModel,
    EvidenceId,
    NonEmptyString,
    OrderId,
    RequestId,
    TraceId,
)
from order_agent_ops.domain.enums import InventoryStatus, OrderStatus, RiskLevel


class OrderItem(DomainModel):
    sku: NonEmptyString
    quantity: int = Field(gt=0)
    unit_price: Decimal = Field(ge=0)


class OrderRequest(DomainModel):
    user_id: NonEmptyString
    items: list[OrderItem] = Field(min_length=1)
    total_amount: Decimal = Field(ge=0)
    idempotency_key: NonEmptyString


class OrderRecord(DomainModel):
    request_id: RequestId
    order_id: OrderId
    trace_id: TraceId
    user_id: NonEmptyString
    items: list[OrderItem] = Field(min_length=1)
    total_amount: Decimal = Field(ge=0)
    idempotency_key: NonEmptyString
    status: OrderStatus
    created_at: AwareDatetime
    updated_at: AwareDatetime


class InventoryCheckResult(DomainModel):
    sku: NonEmptyString
    requested_quantity: int = Field(gt=0)
    status: InventoryStatus
    available_quantity: int | None = Field(default=None, ge=0)
    reason: NonEmptyString
    evidence_ids: list[EvidenceId] = Field(default_factory=list)


class RiskCheckResult(DomainModel):
    user_id: NonEmptyString
    risk_score: float = Field(ge=0)
    risk_level: RiskLevel
    reason: NonEmptyString
    evidence_ids: list[EvidenceId] = Field(default_factory=list)
    requires_manual_review: bool = False
