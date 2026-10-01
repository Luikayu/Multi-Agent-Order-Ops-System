"""Deterministic anomaly rules over local metrics and run records."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime

from pydantic import AwareDatetime, Field

from order_agent_ops.domain.agents import AgentRunRecord, ToolCallRecord
from order_agent_ops.domain.base import DomainModel, NonEmptyString
from order_agent_ops.domain.enums import ActionRiskLevel, OrderStatus, RunStatus
from order_agent_ops.domain.orders import OrderRecord
from order_agent_ops.models.gateway import ModelCallRecord, ModelCallStatus
from order_agent_ops.models.provider import ModelTaskType
from order_agent_ops.telemetry.metrics import MetricSummary


ORDER_LATENCY_METRIC = "order-workflow.order.request"
INVENTORY_LATENCY_METRIC = "inventory-agent.inventory.check"

MODEL_TASK_OBJECTS = {
    ModelTaskType.ORDER_COORDINATOR.value: "order-coordinator-agent",
    ModelTaskType.INVENTORY_AGENT.value: "inventory-agent",
    ModelTaskType.RISK_AGENT.value: "risk-agent",
    ModelTaskType.SHOPPING_ANALYZE.value: "shopping-assistant-agent",
    ModelTaskType.SHOPPING_CATALOG_SEARCH.value: "shopping-assistant-agent",
    ModelTaskType.SHOPPING_RANK.value: "shopping-assistant-agent",
    ModelTaskType.OPS_GUARDIAN.value: "ops-guardian-agent",
}


class AnomalyThresholds(DomainModel):
    """Configurable thresholds for the initial detector rules."""

    order_p95_ms: float = Field(default=2000, gt=0)
    inventory_p95_ms: float = Field(default=2000, gt=0)
    consecutive_failures: int = Field(default=3, ge=1)
    pending_orders: int = Field(default=3, ge=0)
    max_retry_count: int = Field(default=1, ge=1)


class RuleViolation(DomainModel):
    """A rule result before it is persisted as an incident."""

    object_id: NonEmptyString
    rule: NonEmptyString
    summary: NonEmptyString
    occurred_at: AwareDatetime
    source_refs: list[NonEmptyString] = Field(min_length=1)


class HandoffObservation(DomainModel):
    """Raw handoff observation; trace_id may be absent so the rule can detect it."""

    handoff_id: NonEmptyString
    source_agent: NonEmptyString
    target_agent: NonEmptyString
    trace_id: str | None = None
    occurred_at: AwareDatetime


class DangerousActionObservation(DomainModel):
    """A dangerous action attempt observed before stage-14 execution services exist."""

    attempt_id: NonEmptyString
    action: NonEmptyString
    target: NonEmptyString
    risk_level: ActionRiskLevel
    approved: bool
    occurred_at: AwareDatetime


def _metric_ref(name: str, summary: MetricSummary) -> str:
    return f"metric:{name}:count={summary.count}:p95={summary.p95_duration_ms}"


def latency_violations(
    metrics: Mapping[str, MetricSummary],
    thresholds: AnomalyThresholds,
    *,
    occurred_at: datetime,
) -> list[RuleViolation]:
    """Detect order/inventory latency, preferring the more specific inventory signal."""

    order = metrics.get(ORDER_LATENCY_METRIC)
    inventory = metrics.get(INVENTORY_LATENCY_METRIC)
    order_slow = (
        order is not None
        and order.p95_duration_ms is not None
        and order.p95_duration_ms > thresholds.order_p95_ms
    )
    inventory_slow = (
        inventory is not None
        and inventory.p95_duration_ms is not None
        and inventory.p95_duration_ms > thresholds.inventory_p95_ms
    )

    if inventory_slow and inventory is not None:
        refs = [_metric_ref(INVENTORY_LATENCY_METRIC, inventory)]
        if order_slow and order is not None:
            refs.append(_metric_ref(ORDER_LATENCY_METRIC, order))
        return [
            RuleViolation(
                object_id="inventory-agent",
                rule="inventory_p95_latency",
                summary=(
                    "InventoryAgent P95 latency "
                    f"{inventory.p95_duration_ms:.1f}ms exceeded "
                    f"{thresholds.inventory_p95_ms:.1f}ms"
                ),
                occurred_at=occurred_at,
                source_refs=refs,
            )
        ]

    if order_slow and order is not None:
        return [
            RuleViolation(
                object_id="order-workflow",
                rule="order_p95_latency",
                summary=(
                    f"Order P95 latency {order.p95_duration_ms:.1f}ms exceeded "
                    f"{thresholds.order_p95_ms:.1f}ms"
                ),
                occurred_at=occurred_at,
                source_refs=[_metric_ref(ORDER_LATENCY_METRIC, order)],
            )
        ]
    return []


def pending_order_violations(
    orders: Sequence[OrderRecord],
    thresholds: AnomalyThresholds,
    *,
    occurred_at: datetime,
) -> list[RuleViolation]:
    pending = [order for order in orders if order.status is OrderStatus.PENDING]
    if len(pending) <= thresholds.pending_orders:
        return []
    return [
        RuleViolation(
            object_id="order-workflow",
            rule="pending_order_count",
            summary=(
                f"Pending order count {len(pending)} exceeded "
                f"{thresholds.pending_orders}"
            ),
            occurred_at=occurred_at,
            source_refs=[f"order:{order.order_id}" for order in pending],
        )
    ]


def _run_identity(run: AgentRunRecord | ToolCallRecord) -> tuple[str, str]:
    if isinstance(run, AgentRunRecord):
        return run.agent_name, f"agent-run:{run.run_id}"
    return run.tool_name, f"tool-call:{run.call_id}"


def run_record_violations(
    agent_runs: Sequence[AgentRunRecord],
    tool_calls: Sequence[ToolCallRecord],
    thresholds: AnomalyThresholds,
    *,
    occurred_at: datetime,
) -> list[RuleViolation]:
    violations: list[RuleViolation] = []
    grouped: dict[
        str, list[AgentRunRecord | ToolCallRecord]
    ] = defaultdict(list)
    for record in (*agent_runs, *tool_calls):
        object_id, _ = _run_identity(record)
        grouped[object_id].append(record)

    failure_statuses = {
        RunStatus.ERROR,
        RunStatus.TIMEOUT,
        RunStatus.INVALID_OUTPUT,
    }
    for object_id, records in grouped.items():
        records.sort(key=lambda record: record.started_at)
        trailing_failures: list[AgentRunRecord | ToolCallRecord] = []
        for record in reversed(records):
            if record.status not in failure_statuses:
                break
            trailing_failures.append(record)
        if len(trailing_failures) >= thresholds.consecutive_failures:
            triggering = list(reversed(trailing_failures))
            violations.append(
                RuleViolation(
                    object_id=object_id,
                    rule="consecutive_failures",
                    summary=(
                        f"{object_id} has {len(trailing_failures)} "
                        "consecutive failures"
                    ),
                    occurred_at=occurred_at,
                    source_refs=[_run_identity(record)[1] for record in triggering],
                )
            )

        invalid = [
            record for record in records if record.status is RunStatus.INVALID_OUTPUT
        ]
        if invalid:
            violations.append(
                RuleViolation(
                    object_id=object_id,
                    rule="schema_validation_failure",
                    summary=f"{object_id} produced output that failed schema validation",
                    occurred_at=occurred_at,
                    source_refs=[_run_identity(record)[1] for record in invalid],
                )
            )

        exhausted = [
            record
            for record in records
            if record.retry_count >= thresholds.max_retry_count
        ]
        if exhausted:
            violations.append(
                RuleViolation(
                    object_id=object_id,
                    rule="max_retries_reached",
                    summary=(
                        f"{object_id} reached the maximum retry count "
                        f"of {thresholds.max_retry_count}"
                    ),
                    occurred_at=occurred_at,
                    source_refs=[_run_identity(record)[1] for record in exhausted],
                )
            )
    return violations


def model_call_violations(
    model_calls: Sequence[ModelCallRecord],
    thresholds: AnomalyThresholds,
    *,
    occurred_at: datetime,
) -> list[RuleViolation]:
    """Detect invalid model output and exhausted retries at their source."""

    violations: list[RuleViolation] = []
    grouped: dict[str, list[ModelCallRecord]] = defaultdict(list)
    for call in model_calls:
        grouped[MODEL_TASK_OBJECTS.get(call.task_type, "model-gateway")].append(call)

    for object_id, calls in grouped.items():
        invalid = [
            call for call in calls if call.status is ModelCallStatus.INVALID_OUTPUT
        ]
        if invalid:
            violations.append(
                RuleViolation(
                    object_id=object_id,
                    rule="schema_validation_failure",
                    summary=f"{object_id} model output failed schema validation",
                    occurred_at=occurred_at,
                    source_refs=[f"model-call:{call.call_id}" for call in invalid],
                )
            )

        exhausted = [
            call
            for call in calls
            if call.retry_count >= thresholds.max_retry_count
        ]
        if exhausted:
            violations.append(
                RuleViolation(
                    object_id=object_id,
                    rule="max_retries_reached",
                    summary=(
                        f"{object_id} reached the maximum retry count "
                        f"of {thresholds.max_retry_count}"
                    ),
                    occurred_at=occurred_at,
                    source_refs=[f"model-call:{call.call_id}" for call in exhausted],
                )
            )
    return violations


def handoff_violations(
    handoffs: Sequence[HandoffObservation],
) -> list[RuleViolation]:
    return [
        RuleViolation(
            object_id=f"{handoff.source_agent}->{handoff.target_agent}",
            rule="handoff_missing_trace_id",
            summary=(
                f"Handoff from {handoff.source_agent} to {handoff.target_agent} "
                "is missing trace_id"
            ),
            occurred_at=handoff.occurred_at,
            source_refs=[f"handoff:{handoff.handoff_id}"],
        )
        for handoff in handoffs
        if handoff.trace_id is None or not handoff.trace_id.strip()
    ]


def dangerous_action_violations(
    attempts: Sequence[DangerousActionObservation],
) -> list[RuleViolation]:
    dangerous = {ActionRiskLevel.HIGH, ActionRiskLevel.CRITICAL}
    return [
        RuleViolation(
            object_id=attempt.target,
            rule="dangerous_action_without_approval",
            summary=(
                f"{attempt.action} was attempted on {attempt.target} "
                "without approval"
            ),
            occurred_at=attempt.occurred_at,
            source_refs=[f"action-attempt:{attempt.attempt_id}"],
        )
        for attempt in attempts
        if attempt.risk_level in dangerous and not attempt.approved
    ]
