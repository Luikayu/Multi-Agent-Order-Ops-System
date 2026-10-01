from decimal import Decimal
from threading import Barrier

import pytest

from order_agent_ops.agents import (
    InventoryAgent,
    OrderCoordinationResult,
    OrderCoordinatorAgent,
    RiskAgent,
)
from order_agent_ops.business.inventory_adapter import InventoryAdapter
from order_agent_ops.business.risk_profile_adapter import RiskProfileAdapter
from order_agent_ops.domain import (
    InventoryCheckResult,
    InventoryStatus,
    OrderItem,
    OrderRequest,
    RiskCheckResult,
    RiskLevel,
    RunStatus,
)
from order_agent_ops.models import (
    MockBehavior,
    MockModelProvider,
    MockTaskType,
    ModelGateway,
    ProviderInvocationError,
)
from order_agent_ops.telemetry.tracing import SpanStatus


def make_order_request() -> OrderRequest:
    return OrderRequest(
        user_id="USER-001",
        items=[OrderItem(sku="SKU-001", quantity=1, unit_price=Decimal("399.00"))],
        total_amount=Decimal("399.00"),
        idempotency_key="order-attempt-001",
    )


def test_inventory_agent_runs_independently_with_mock_gateway() -> None:
    with ModelGateway(MockModelProvider()) as gateway:
        agent = InventoryAgent(gateway, InventoryAdapter())

        result = agent.run(
            sku="SKU-001",
            requested_quantity=1,
            trace_id="TRACE-INVENTORY-AGENT",
        )

        assert isinstance(result, InventoryCheckResult)
        assert result.status is InventoryStatus.SUFFICIENT
        assert result.available_quantity == 12
        assert result.requested_quantity == 1
        record = agent.list_run_records("TRACE-INVENTORY-AGENT")[0]
        assert record.agent_name == "inventory-agent"
        assert record.agent_version == "v2.0"
        assert record.prompt_version == "inventory-prompt-v2"
        assert record.status is RunStatus.SUCCESS
        assert agent.output_schema is InventoryCheckResult


def test_inventory_agent_never_passes_missing_or_insufficient_stock() -> None:
    with ModelGateway(MockModelProvider()) as gateway:
        agent = InventoryAgent(gateway, InventoryAdapter())

        missing = agent.run(
            sku="SKU-404",
            requested_quantity=1,
            trace_id="TRACE-INVENTORY-MISSING",
        )
        insufficient = agent.run(
            sku="SKU-002",
            requested_quantity=1,
            trace_id="TRACE-INVENTORY-INSUFFICIENT",
        )

        assert missing.status is InventoryStatus.UNKNOWN
        assert missing.available_quantity is None
        assert insufficient.status is InventoryStatus.INSUFFICIENT
        assert insufficient.available_quantity == 0


def test_risk_agent_runs_independently_and_uncertainty_requires_review() -> None:
    provider = MockModelProvider(
        responses={
            MockTaskType.RISK_AGENT: {
                "user_id": "USER-001",
                "risk_score": 0.5,
                "risk_level": "unknown",
                "reason": "Mock response is uncertain",
                "evidence_ids": [],
                "requires_manual_review": False,
            }
        }
    )
    with ModelGateway(provider) as gateway:
        agent = RiskAgent(gateway, RiskProfileAdapter())

        result = agent.run(user_id="USER-001", trace_id="TRACE-RISK-AGENT")

        assert isinstance(result, RiskCheckResult)
        assert result.risk_level is RiskLevel.UNKNOWN
        assert result.requires_manual_review is True
        record = agent.list_run_records("TRACE-RISK-AGENT")[0]
        assert record.agent_name == "risk-agent"
        assert record.status is RunStatus.SUCCESS
        assert agent.output_schema is RiskCheckResult


def test_missing_risk_profile_is_unknown_without_model_guess() -> None:
    provider = MockModelProvider()
    with ModelGateway(provider) as gateway:
        agent = RiskAgent(gateway, RiskProfileAdapter())

        result = agent.run(user_id="USER-404", trace_id="TRACE-RISK-MISSING")

        assert result.risk_level is RiskLevel.UNKNOWN
        assert result.requires_manual_review is True
        assert provider.calls == []


class TimeoutInventoryAdapter:
    def query(self, _sku: str):
        raise TimeoutError("simulated inventory data timeout")


def test_inventory_adapter_timeout_returns_timeout_and_locatable_run() -> None:
    with ModelGateway(MockModelProvider()) as gateway:
        agent = InventoryAgent(gateway, TimeoutInventoryAdapter())

        result = agent.run(
            sku="SKU-001",
            requested_quantity=1,
            trace_id="TRACE-INVENTORY-TIMEOUT",
        )

        assert result.status is InventoryStatus.TIMEOUT
        record = agent.list_run_records("TRACE-INVENTORY-TIMEOUT")[0]
        assert record.status is RunStatus.TIMEOUT
        assert record.error_type == "TimeoutError"
        span = next(
            span
            for span in gateway.trace_recorder.get_trace("TRACE-INVENTORY-TIMEOUT")
            if span.component == "inventory-agent"
        )
        assert span.status is SpanStatus.ERROR
        assert span.exception_type == "TimeoutError"


def test_risk_model_failure_degrades_to_review_and_records_retry() -> None:
    provider = MockModelProvider(
        behaviors={
            MockTaskType.RISK_AGENT: [MockBehavior.ERROR, MockBehavior.ERROR]
        }
    )
    with ModelGateway(provider) as gateway:
        agent = RiskAgent(gateway, RiskProfileAdapter())

        result = agent.run(user_id="USER-001", trace_id="TRACE-RISK-ERROR")

        assert result.risk_level is RiskLevel.UNKNOWN
        assert result.requires_manual_review is True
        record = agent.list_run_records("TRACE-RISK-ERROR")[0]
        assert record.status is RunStatus.ERROR
        assert record.error_type == "ProviderInvocationError"
        assert record.retry_count == 1
        assert len(provider.calls) == 2


class BarrierInventoryAdapter:
    def __init__(self, barrier: Barrier) -> None:
        self._barrier = barrier
        self._delegate = InventoryAdapter()

    def query(self, sku: str):
        self._barrier.wait(timeout=1)
        return self._delegate.query(sku)


class BarrierRiskProfileAdapter:
    def __init__(self, barrier: Barrier) -> None:
        self._barrier = barrier
        self._delegate = RiskProfileAdapter()

    def query(self, user_id: str):
        self._barrier.wait(timeout=1)
        return self._delegate.query(user_id)


def test_coordinator_dispatches_checks_in_parallel_on_one_trace() -> None:
    barrier = Barrier(2)
    with ModelGateway(MockModelProvider()) as gateway:
        inventory_agent = InventoryAgent(
            gateway,
            BarrierInventoryAdapter(barrier),
        )
        risk_agent = RiskAgent(
            gateway,
            BarrierRiskProfileAdapter(barrier),
        )
        coordinator = OrderCoordinatorAgent(
            gateway,
            inventory_agent,
            risk_agent,
        )

        result = coordinator.run(
            make_order_request(),
            trace_id="TRACE-COORDINATION",
        )

        assert isinstance(result, OrderCoordinationResult)
        assert result.trace_id == "TRACE-COORDINATION"
        assert result.plan.request_valid is True
        assert result.inventory_results[0].status is InventoryStatus.SUFFICIENT
        assert result.risk_result is not None
        assert result.risk_result.risk_level is RiskLevel.LOW

        spans = gateway.trace_recorder.get_trace("TRACE-COORDINATION")
        coordinator_span = next(
            span for span in spans if span.component == "order-coordinator-agent"
        )
        inventory_span = next(
            span for span in spans if span.component == "inventory-agent"
        )
        risk_span = next(span for span in spans if span.component == "risk-agent")

        assert inventory_span.trace_id == risk_span.trace_id == "TRACE-COORDINATION"
        assert inventory_span.span_id != risk_span.span_id
        assert inventory_span.parent_span_id == coordinator_span.span_id
        assert risk_span.parent_span_id == coordinator_span.span_id
        assert coordinator.list_run_records("TRACE-COORDINATION")[0].status is RunStatus.SUCCESS
        assert inventory_agent.list_run_records("TRACE-COORDINATION")[0].status is RunStatus.SUCCESS
        assert risk_agent.list_run_records("TRACE-COORDINATION")[0].status is RunStatus.SUCCESS


def test_coordinator_model_failure_is_rethrown_and_locatable() -> None:
    provider = MockModelProvider(
        behaviors={
            MockTaskType.ORDER_COORDINATOR: [MockBehavior.ERROR, MockBehavior.ERROR]
        }
    )
    with ModelGateway(provider) as gateway:
        inventory_agent = InventoryAgent(gateway, InventoryAdapter())
        risk_agent = RiskAgent(gateway, RiskProfileAdapter())
        coordinator = OrderCoordinatorAgent(
            gateway,
            inventory_agent,
            risk_agent,
        )

        with pytest.raises(ProviderInvocationError, match="Provider mock failed"):
            coordinator.run(
                make_order_request(),
                trace_id="TRACE-COORDINATOR-ERROR",
            )

        record = coordinator.list_run_records("TRACE-COORDINATOR-ERROR")[0]
        assert record.status is RunStatus.ERROR
        assert record.error_type == "ProviderInvocationError"
        assert record.retry_count == 1


def test_parallel_inventory_retries_are_attributed_to_the_correct_run() -> None:
    provider = MockModelProvider(
        behaviors={
            MockTaskType.INVENTORY_AGENT: [
                MockBehavior.ERROR,
                MockBehavior.SUCCESS,
                MockBehavior.SUCCESS,
            ]
        }
    )
    request = OrderRequest(
        user_id="USER-001",
        items=[
            OrderItem(sku="SKU-001", quantity=1, unit_price=Decimal("399.00")),
            OrderItem(sku="SKU-002", quantity=1, unit_price=Decimal("159.00")),
        ],
        total_amount=Decimal("558.00"),
        idempotency_key="order-attempt-multi-item",
    )

    with ModelGateway(provider) as gateway:
        inventory_agent = InventoryAgent(gateway, InventoryAdapter())
        coordinator = OrderCoordinatorAgent(
            gateway,
            inventory_agent,
            RiskAgent(gateway, RiskProfileAdapter()),
        )

        result = coordinator.run(request, trace_id="TRACE-MULTI-INVENTORY")
        run_records = inventory_agent.list_run_records("TRACE-MULTI-INVENTORY")
        model_calls = [
            call
            for call in gateway.list_calls("TRACE-MULTI-INVENTORY")
            if call.task_type == MockTaskType.INVENTORY_AGENT
        ]

        assert len(result.inventory_results) == 2
        assert sorted(record.retry_count for record in run_records) == [0, 1]
        assert {call.agent_run_id for call in model_calls} == {
            record.run_id for record in run_records
        }
