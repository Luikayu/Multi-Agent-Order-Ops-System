"""Order coordinator that plans and dispatches business checks in parallel."""

import json
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from typing import ClassVar, Literal

from pydantic import Field, model_validator

from order_agent_ops.agents.base import BaseAgent
from order_agent_ops.agents.inventory_agent import InventoryAgent
from order_agent_ops.agents.risk_agent import RiskAgent
from order_agent_ops.domain.base import DomainModel, NonEmptyString, TraceId
from order_agent_ops.domain.orders import (
    InventoryCheckResult,
    OrderRequest,
    RiskCheckResult,
)
from order_agent_ops.models.provider import ModelTaskType


CheckName = Literal["inventory", "risk"]


class CoordinatorPlan(DomainModel):
    request_valid: bool
    required_checks: list[CheckName]
    reason: NonEmptyString

    @model_validator(mode="after")
    def valid_request_requires_both_checks(self) -> "CoordinatorPlan":
        if self.request_valid:
            if len(self.required_checks) != 2 or set(self.required_checks) != {
                "inventory",
                "risk",
            }:
                raise ValueError(
                    "A valid order must include inventory and risk checks exactly once"
                )
        return self


class OrderCoordinationResult(DomainModel):
    trace_id: TraceId
    plan: CoordinatorPlan
    inventory_results: list[InventoryCheckResult]
    risk_result: RiskCheckResult | None

    @model_validator(mode="after")
    def valid_plan_requires_results(self) -> "OrderCoordinationResult":
        if self.plan.request_valid:
            if not self.inventory_results or self.risk_result is None:
                raise ValueError("A valid coordination plan requires both check results")
        return self


class OrderCoordinatorAgent(BaseAgent):
    name = "order-coordinator-agent"
    version = "v1.0"
    prompt_version = "order-coordinator-prompt-v2"
    output_schema: ClassVar[type[OrderCoordinationResult]] = OrderCoordinationResult

    def __init__(
        self,
        model_gateway,
        inventory_agent: InventoryAgent,
        risk_agent: RiskAgent,
        **kwargs,
    ) -> None:
        super().__init__(model_gateway, **kwargs)
        self.inventory_agent = inventory_agent
        self.risk_agent = risk_agent
        for child in (inventory_agent, risk_agent):
            if child.trace_recorder is not self.trace_recorder:
                raise ValueError("Coordinator and child agents must share one TraceRecorder")

    def run(
        self,
        order_request: OrderRequest,
        *,
        trace_id: str,
    ) -> OrderCoordinationResult:
        with self.track_run(
            trace_id,
            action="coordinator.plan_and_dispatch",
            model_task_type=ModelTaskType.ORDER_COORDINATOR.value,
        ) as run_context:
            plan = self.model_gateway.generate(
                [
                    {
                        "role": "system",
                        "content": (
                            "Validate the already structured order and return exactly the supplied "
                            "check-plan schema with request_valid, required_checks, and reason. A "
                            "request with a non-empty user_id, at least one positive-quantity item, "
                            "non-negative prices and total, and a non-empty idempotency_key is "
                            "valid. Every valid request must use required_checks exactly "
                            "[\"inventory\",\"risk\"]. Valid example: {\"request_valid\":true,"
                            "\"required_checks\":[\"inventory\",\"risk\"],\"reason\":"
                            "\"Structured order fields are valid\"}. For an invalid request set "
                            "request_valid=false, required_checks=[], and explain the invalid "
                            "field. Return JSON only."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            order_request.model_dump(mode="json"),
                            separators=(",", ":"),
                        ),
                    },
                ],
                task_type=ModelTaskType.ORDER_COORDINATOR.value,
                response_schema=CoordinatorPlan,
                prompt_version=self.prompt_version,
                trace_id=trace_id,
                agent_run_id=run_context.run_id,
            )
            if not plan.request_valid:
                return OrderCoordinationResult(
                    trace_id=trace_id,
                    plan=plan,
                    inventory_results=[],
                    risk_result=None,
                )

            worker_count = len(order_request.items) + 1
            with ThreadPoolExecutor(
                max_workers=worker_count,
                thread_name_prefix="business-agent",
            ) as executor:
                inventory_futures = []
                for item in order_request.items:
                    context = copy_context()
                    inventory_futures.append(
                        executor.submit(
                            context.run,
                            self.inventory_agent.run,
                            sku=item.sku,
                            requested_quantity=item.quantity,
                            trace_id=trace_id,
                        )
                    )
                risk_context = copy_context()
                risk_future = executor.submit(
                    risk_context.run,
                    self.risk_agent.run,
                    user_id=order_request.user_id,
                    trace_id=trace_id,
                )
                inventory_results = [future.result() for future in inventory_futures]
                risk_result = risk_future.result()

            return OrderCoordinationResult(
                trace_id=trace_id,
                plan=plan,
                inventory_results=inventory_results,
                risk_result=risk_result,
            )
