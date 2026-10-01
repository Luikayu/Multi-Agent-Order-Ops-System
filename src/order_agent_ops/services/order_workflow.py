"""End-to-end composition for one normal order request."""

from datetime import datetime, timezone
from uuid import uuid4

from order_agent_ops.agents.order_coordinator import OrderCoordinatorAgent
from order_agent_ops.business.order_executor import (
    IdempotencyConflictError,
    OrderExecutorService,
)
from order_agent_ops.business.policy_engine import OrderPolicyEngine, PolicyDecision
from order_agent_ops.domain.base import DomainModel
from order_agent_ops.domain.enums import OrderStatus
from order_agent_ops.domain.orders import OrderRecord, OrderRequest
from order_agent_ops.storage.database import Database
from order_agent_ops.storage.repositories import (
    InsufficientInventoryError,
    InventoryUnavailableError,
    OrderRepository,
)
from order_agent_ops.telemetry.tracing import TraceRecorder


class OrderWorkflowError(RuntimeError):
    """Base error carrying a stable API code and trace reference."""

    error_code = "ORDER_WORKFLOW_ERROR"

    def __init__(self, message: str, *, trace_id: str) -> None:
        super().__init__(message)
        self.trace_id = trace_id


class OrderWorkflowConflictError(OrderWorkflowError):
    error_code = "IDEMPOTENCY_CONFLICT"


class OrderWorkflowUnavailableError(OrderWorkflowError):
    error_code = "ORDER_WORKFLOW_UNAVAILABLE"


class OrderWorkflowResult(DomainModel):
    order: OrderRecord
    idempotent_replay: bool


class OrderWorkflow:
    """Compose coordinator, policy, and executor while preserving safety boundaries."""

    def __init__(
        self,
        database: Database,
        order_repository: OrderRepository,
        coordinator: OrderCoordinatorAgent,
        policy_engine: OrderPolicyEngine,
        executor: OrderExecutorService,
        trace_recorder: TraceRecorder,
    ) -> None:
        self._database = database
        self._orders = order_repository
        self._coordinator = coordinator
        self._policy = policy_engine
        self._executor = executor
        self.trace_recorder = trace_recorder

    def submit(
        self,
        order_request: OrderRequest,
        *,
        trace_id: str | None = None,
    ) -> OrderWorkflowResult:
        trace_id = trace_id or self.trace_recorder.new_trace_id()
        existing = self._orders.get_by_idempotency_key(
            order_request.idempotency_key
        )
        if existing is not None:
            self._require_matching_request(existing, order_request, trace_id)
            return OrderWorkflowResult(order=existing, idempotent_replay=True)

        request_id = f"REQ-{uuid4().hex}"
        order_id = f"ORD-{uuid4().hex}"
        with self.trace_recorder.span(trace_id, "order-workflow", "order.request"):
            try:
                coordination = self._coordinator.run(
                    order_request,
                    trace_id=trace_id,
                )
            except Exception as error:
                raise OrderWorkflowUnavailableError(
                    "Order coordination failed",
                    trace_id=trace_id,
                ) from error

            with self.trace_recorder.span(
                trace_id, "order-policy", "policy.decide"
            ):
                decision = self._policy.decide(
                    coordination.inventory_results,
                    coordination.risk_result,
                )

            references = self._evidence_references(coordination)
            if decision.allow_creation:
                result = self._execute_approved(
                    order_request,
                    decision,
                    request_id=request_id,
                    order_id=order_id,
                    trace_id=trace_id,
                    references=references,
                )
                return result

            with self.trace_recorder.span(
                trace_id, "order-workflow", "decision.persist"
            ):
                order, replay = self._persist_non_approved(
                    order_request,
                    decision,
                    request_id=request_id,
                    order_id=order_id,
                    trace_id=trace_id,
                    references=references,
                )
            return OrderWorkflowResult(order=order, idempotent_replay=replay)

    def _execute_approved(
        self,
        order_request: OrderRequest,
        decision: PolicyDecision,
        *,
        request_id: str,
        order_id: str,
        trace_id: str,
        references: list[str],
    ) -> OrderWorkflowResult:
        try:
            with self.trace_recorder.span(
                trace_id, "order-executor", "order.execute"
            ):
                result = self._executor.execute(
                    order_request,
                    decision,
                    request_id=request_id,
                    order_id=order_id,
                    trace_id=trace_id,
                )
        except InsufficientInventoryError:
            fallback = PolicyDecision(
                status=OrderStatus.REJECTED,
                allow_creation=False,
                reason="Inventory became insufficient before reservation",
            )
            order, replay = self._persist_non_approved(
                order_request,
                fallback,
                request_id=request_id,
                order_id=order_id,
                trace_id=trace_id,
                references=references,
            )
            return OrderWorkflowResult(order=order, idempotent_replay=replay)
        except InventoryUnavailableError:
            fallback = PolicyDecision(
                status=OrderStatus.PENDING,
                allow_creation=False,
                reason="Inventory disappeared before reservation",
            )
            order, replay = self._persist_non_approved(
                order_request,
                fallback,
                request_id=request_id,
                order_id=order_id,
                trace_id=trace_id,
                references=references,
            )
            return OrderWorkflowResult(order=order, idempotent_replay=replay)
        except IdempotencyConflictError as error:
            raise OrderWorkflowConflictError(
                str(error), trace_id=trace_id
            ) from error
        except Exception as error:
            raise OrderWorkflowUnavailableError(
                "Approved order execution failed",
                trace_id=trace_id,
            ) from error

        return OrderWorkflowResult(
            order=result.order,
            idempotent_replay=result.idempotent_replay,
        )

    def _persist_non_approved(
        self,
        order_request: OrderRequest,
        decision: PolicyDecision,
        *,
        request_id: str,
        order_id: str,
        trace_id: str,
        references: list[str],
    ) -> tuple[OrderRecord, bool]:
        if decision.status is OrderStatus.APPROVED:
            raise ValueError("Approved decisions must use OrderExecutorService")

        with self._database.transaction(immediate=True) as connection:
            existing = self._orders.get_by_idempotency_key_with_connection(
                connection, order_request.idempotency_key
            )
            if existing is not None:
                self._require_matching_request(existing, order_request, trace_id)
                return existing, True

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
            order = self._orders.transition_status_with_connection(
                connection,
                order_id,
                OrderStatus.CHECKING,
                trigger_component="order-workflow",
                reason="Agent checks completed",
                references=references,
            )
            order = self._orders.transition_status_with_connection(
                connection,
                order_id,
                decision.status,
                trigger_component="order-policy",
                reason=decision.reason,
                references=references,
            )
            return order, False

    @staticmethod
    def _require_matching_request(
        existing: OrderRecord,
        order_request: OrderRequest,
        trace_id: str,
    ) -> None:
        if not OrderExecutorService.matches_request(existing, order_request):
            raise OrderWorkflowConflictError(
                "Idempotency key was already used for different order input",
                trace_id=trace_id,
            )

    @staticmethod
    def _evidence_references(coordination) -> list[str]:
        references: list[str] = []
        for inventory_result in coordination.inventory_results:
            references.extend(inventory_result.evidence_ids)
        if coordination.risk_result is not None:
            references.extend(coordination.risk_result.evidence_ids)
        return list(dict.fromkeys(references))
