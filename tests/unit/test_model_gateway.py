from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict

from order_agent_ops.domain import DiagnosisResult, InventoryCheckResult, RiskCheckResult
from order_agent_ops.models import (
    MockBehavior,
    MockModelProvider,
    MockTaskType,
    ModelCallStatus,
    ModelGateway,
    ModelResponseValidationError,
    ModelTimeoutError,
    ProviderInvocationError,
)
from order_agent_ops.models.errors import InvalidModelResponseError


MESSAGES = [{"role": "user", "content": "Return the configured mock result"}]


class CoordinatorResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_valid: bool
    required_checks: list[Literal["inventory", "risk"]]
    reason: str


def generate(
    gateway: ModelGateway,
    task_type: str,
    response_schema: type[BaseModel],
    *,
    trace_id: str = "TRACE-MODEL-001",
):
    return gateway.generate(
        MESSAGES,
        task_type=task_type,
        response_schema=response_schema,
        prompt_version="prompt-v1",
        trace_id=trace_id,
    )


def test_mock_returns_structured_results_for_all_four_agents() -> None:
    provider = MockModelProvider()

    with ModelGateway(provider) as gateway:
        coordinator = generate(
            gateway,
            MockTaskType.ORDER_COORDINATOR,
            CoordinatorResult,
            trace_id="TRACE-COORDINATOR",
        )
        inventory = generate(
            gateway,
            MockTaskType.INVENTORY_AGENT,
            InventoryCheckResult,
            trace_id="TRACE-INVENTORY",
        )
        risk = generate(
            gateway,
            MockTaskType.RISK_AGENT,
            RiskCheckResult,
            trace_id="TRACE-RISK",
        )
        diagnosis = generate(
            gateway,
            MockTaskType.OPS_GUARDIAN,
            DiagnosisResult,
            trace_id="TRACE-OPS",
        )

        assert coordinator.request_valid is True
        assert coordinator.required_checks == ["inventory", "risk"]
        assert inventory.status.value == "sufficient"
        assert risk.risk_level.value == "low"
        assert diagnosis.root_cause_candidates[0].confidence == 0.82
        assert len(gateway.list_calls()) == 4
        assert all(
            call.status is ModelCallStatus.SUCCESS for call in gateway.list_calls()
        )


def test_one_provider_failure_is_retried_then_succeeds() -> None:
    provider = MockModelProvider(
        behaviors={
            MockTaskType.INVENTORY_AGENT: [
                MockBehavior.ERROR,
                MockBehavior.SUCCESS,
            ]
        }
    )

    with ModelGateway(provider, max_retries=1) as gateway:
        result = generate(
            gateway,
            MockTaskType.INVENTORY_AGENT,
            InventoryCheckResult,
        )
        calls = gateway.list_calls("TRACE-MODEL-001")

        assert result.status.value == "sufficient"
        assert provider.calls == ["inventory_agent", "inventory_agent"]
        assert [call.status for call in calls] == [
            ModelCallStatus.ERROR,
            ModelCallStatus.SUCCESS,
        ]
        assert [call.retry_count for call in calls] == [0, 1]
        assert len(gateway.trace_recorder.get_trace("TRACE-MODEL-001")) == 2


def test_continuous_provider_failure_stops_after_one_retry() -> None:
    provider = MockModelProvider(
        behaviors={
            MockTaskType.INVENTORY_AGENT: [
                MockBehavior.ERROR,
                MockBehavior.ERROR,
                MockBehavior.SUCCESS,
            ]
        }
    )

    with ModelGateway(provider, max_retries=1) as gateway:
        with pytest.raises(ProviderInvocationError):
            generate(
                gateway,
                MockTaskType.INVENTORY_AGENT,
                InventoryCheckResult,
            )

        assert provider.calls == ["inventory_agent", "inventory_agent"]
        assert len(gateway.list_calls()) == 2
        assert all(
            call.status is ModelCallStatus.ERROR for call in gateway.list_calls()
        )


def test_invalid_json_is_retried_once_then_validated() -> None:
    provider = MockModelProvider(
        behaviors={
            MockTaskType.RISK_AGENT: [
                MockBehavior.INVALID_JSON,
                MockBehavior.SUCCESS,
            ]
        }
    )

    with ModelGateway(provider) as gateway:
        result = generate(gateway, MockTaskType.RISK_AGENT, RiskCheckResult)

        assert result.risk_level.value == "low"
        assert [call.status for call in gateway.list_calls()] == [
            ModelCallStatus.INVALID_OUTPUT,
            ModelCallStatus.SUCCESS,
        ]


def test_repeated_invalid_json_raises_internal_error() -> None:
    provider = MockModelProvider(
        behaviors={
            MockTaskType.RISK_AGENT: [
                MockBehavior.INVALID_JSON,
                MockBehavior.INVALID_JSON,
            ]
        }
    )

    with ModelGateway(provider) as gateway:
        with pytest.raises(InvalidModelResponseError):
            generate(gateway, MockTaskType.RISK_AGENT, RiskCheckResult)

        assert len(provider.calls) == 2


def test_pydantic_validation_failure_is_retried_and_bounded() -> None:
    provider = MockModelProvider(
        responses={
            MockTaskType.INVENTORY_AGENT: {
                "sku": "SKU-001",
                "requested_quantity": 0,
                "status": "sufficient",
                "available_quantity": 10,
                "reason": "Invalid quantity for schema test",
                "evidence_ids": [],
            }
        }
    )

    with ModelGateway(provider) as gateway:
        with pytest.raises(ModelResponseValidationError):
            generate(
                gateway,
                MockTaskType.INVENTORY_AGENT,
                InventoryCheckResult,
            )

        assert len(provider.calls) == 2
        assert all(
            call.status is ModelCallStatus.INVALID_OUTPUT
            for call in gateway.list_calls()
        )


def test_real_gateway_deadline_maps_to_internal_timeout() -> None:
    provider = MockModelProvider(latency_seconds=0.05)

    with ModelGateway(provider, timeout_seconds=0.005, max_retries=0) as gateway:
        with pytest.raises(ModelTimeoutError):
            generate(
                gateway,
                MockTaskType.INVENTORY_AGENT,
                InventoryCheckResult,
            )

        record = gateway.list_calls()[0]
        assert record.status is ModelCallStatus.TIMEOUT
        assert record.error_type == "ModelTimeoutError"


def test_mock_timeout_behavior_maps_to_internal_timeout() -> None:
    provider = MockModelProvider(
        behaviors={MockTaskType.INVENTORY_AGENT: [MockBehavior.TIMEOUT]}
    )

    with ModelGateway(provider, max_retries=0) as gateway:
        with pytest.raises(ModelTimeoutError):
            generate(
                gateway,
                MockTaskType.INVENTORY_AGENT,
                InventoryCheckResult,
            )


def test_configured_mock_response_and_call_telemetry() -> None:
    provider = MockModelProvider(
        model_name="custom-mock",
        responses={
            MockTaskType.INVENTORY_AGENT: {
                "sku": "SKU-009",
                "requested_quantity": 3,
                "status": "insufficient",
                "available_quantity": 1,
                "reason": "Configured test inventory",
                "evidence_ids": [],
            }
        },
    )

    with ModelGateway(provider) as gateway:
        result = gateway.generate(
            MESSAGES,
            task_type=MockTaskType.INVENTORY_AGENT,
            response_schema=InventoryCheckResult,
            prompt_version="inventory-prompt-v3",
            trace_id="TRACE-TELEMETRY",
        )
        record = gateway.list_calls("TRACE-TELEMETRY")[0]
        summary = gateway.metrics.summary("model.mock.inventory_agent")

        assert result.status.value == "insufficient"
        assert record.provider == "mock"
        assert record.model_name == "custom-mock"
        assert record.prompt_version == "inventory-prompt-v3"
        assert record.duration_ms >= 0
        assert record.status is ModelCallStatus.SUCCESS
        assert record.attempt == 1
        assert summary.count == 1
        assert summary.error_count == 0


@pytest.mark.parametrize(("timeout", "retries"), [(0, 1), (1, -1), (1, 2)])
def test_gateway_rejects_unbounded_or_invalid_retry_configuration(
    timeout: float, retries: int
) -> None:
    with pytest.raises(ValueError):
        ModelGateway(
            MockModelProvider(),
            timeout_seconds=timeout,
            max_retries=retries,
        )
