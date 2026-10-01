"""Safe replay of existing PENDING orders."""

from __future__ import annotations

from threading import RLock

from order_agent_ops.agents.order_coordinator import OrderCoordinatorAgent
from order_agent_ops.business.order_executor import OrderExecutorService
from order_agent_ops.business.policy_engine import OrderPolicyEngine
from order_agent_ops.domain.base import DomainModel, TraceId
from order_agent_ops.domain.enums import OrderStatus
from order_agent_ops.domain.orders import OrderRecord, OrderRequest
from order_agent_ops.ops.approval_service import ApprovalService
from order_agent_ops.storage.repositories import (
    InsufficientInventoryError,
    InventoryUnavailableError,
    OrderRepository,
    RecordNotFoundError,
)
from order_agent_ops.telemetry.tracing import TraceRecorder


class OrderNotReplayableError(ValueError):
    pass


class ReplayResult(DomainModel):
    order: OrderRecord
    replay_trace_id: TraceId
    idempotent_replay: bool


class ReplayService:
    """Re-check an existing order and reuse its immutable idempotency identity."""

    def __init__(
        self,
        orders: OrderRepository,
        coordinator: OrderCoordinatorAgent,
        policy: OrderPolicyEngine,
        executor: OrderExecutorService,
        traces: TraceRecorder,
        approvals: ApprovalService,
    ) -> None:
        self._orders = orders
        self._coordinator = coordinator
        self._policy = policy
        self._executor = executor
        self._traces = traces
        self._approvals = approvals
        self._lock = RLock()

    def replay(
        self,
        order_id: str,
        *,
        approval_id: str | None = None,
        approval_token: str | None = None,
        trace_id: str | None = None,
    ) -> ReplayResult:
        replay_trace_id = trace_id or self._traces.new_trace_id()
        with self._lock:
            existing = self._orders.get(order_id)
            if existing is None:
                raise RecordNotFoundError(f"Order not found: {order_id}")
            if existing.status is OrderStatus.COMPLETED:
                return ReplayResult(
                    order=existing,
                    replay_trace_id=replay_trace_id,
                    idempotent_replay=True,
                )
            if existing.status is not OrderStatus.PENDING:
                raise OrderNotReplayableError(
                    f"Only PENDING orders can be replayed, got {existing.status.value}"
                )

            self._approvals.validate_and_consume(
                approval_id=approval_id,
                approval_token=approval_token,
                action="replay",
                target=order_id,
                target_version=None,
            )

            with self._traces.span(
                replay_trace_id,
                "replay-service",
                "order.replay",
            ):
                self._orders.transition_status(
                    order_id,
                    OrderStatus.REPLAYING,
                    trigger_component="replay-service",
                    reason="Pending order replay started",
                    references=[replay_trace_id],
                )
                checking = self._orders.transition_status(
                    order_id,
                    OrderStatus.CHECKING,
                    trigger_component="replay-service",
                    reason="Replay is re-running business checks",
                    references=[replay_trace_id],
                )
                request = OrderRequest(
                    user_id=checking.user_id,
                    items=checking.items,
                    total_amount=checking.total_amount,
                    idempotency_key=checking.idempotency_key,
                )
                try:
                    coordination = self._coordinator.run(
                        request,
                        trace_id=replay_trace_id,
                    )
                    decision = self._policy.decide(
                        coordination.inventory_results,
                        coordination.risk_result,
                    )
                except Exception:
                    pending = self._orders.transition_status(
                        order_id,
                        OrderStatus.PENDING,
                        trigger_component="replay-service",
                        reason="Replay checks failed; order remains retryable",
                        references=[replay_trace_id],
                    )
                    return ReplayResult(
                        order=pending,
                        replay_trace_id=replay_trace_id,
                        idempotent_replay=False,
                    )

                references: list[str] = []
                for inventory_result in coordination.inventory_results:
                    references.extend(inventory_result.evidence_ids)
                if coordination.risk_result is not None:
                    references.extend(coordination.risk_result.evidence_ids)
                references = list(dict.fromkeys(references))

                if decision.allow_creation:
                    try:
                        execution = self._executor.execute_replay(
                            order_id,
                            decision,
                            replay_trace_id=replay_trace_id,
                            references=references,
                        )
                    except InsufficientInventoryError:
                        order = self._orders.transition_status(
                            order_id,
                            OrderStatus.REJECTED,
                            trigger_component="replay-service",
                            reason="Inventory became insufficient during replay",
                            references=[replay_trace_id],
                        )
                    except InventoryUnavailableError:
                        order = self._orders.transition_status(
                            order_id,
                            OrderStatus.PENDING,
                            trigger_component="replay-service",
                            reason="Inventory became unavailable during replay",
                            references=[replay_trace_id],
                        )
                    else:
                        order = execution.order
                else:
                    order = self._orders.transition_status(
                        order_id,
                        decision.status,
                        trigger_component="replay-service",
                        reason=decision.reason,
                        references=[replay_trace_id, *references],
                    )

                return ReplayResult(
                    order=order,
                    replay_trace_id=replay_trace_id,
                    idempotent_replay=False,
                )
