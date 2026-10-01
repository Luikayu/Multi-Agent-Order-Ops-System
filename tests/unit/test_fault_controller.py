import pytest

from order_agent_ops.business.inventory_adapter import InventoryAdapter
from order_agent_ops.faults import (
    FaultController,
    FaultInjectingInventoryAdapter,
    FaultInjectingModelProvider,
    FaultScenario,
    FaultTarget,
    InjectedFaultError,
)
from order_agent_ops.models import MockModelProvider
from order_agent_ops.models.provider import ModelTaskType


def test_default_config_defines_normal_and_latency_versions() -> None:
    controller = FaultController.from_yaml()

    normal = controller.scenarios["inventory_v20_normal"]
    latency = controller.scenarios["inventory_v21_latency"]

    assert normal.component_version == "v2.0"
    assert 100 <= normal.delay_ms <= 200
    assert latency.component_version == "v2.1"
    assert latency.delay_ms == 2300


def test_inventory_latency_can_be_enabled_and_disabled() -> None:
    slept: list[float] = []
    controller = FaultController.from_yaml(sleeper=slept.append)
    delegate = InventoryAdapter()
    adapter = FaultInjectingInventoryAdapter(delegate, controller)

    assert adapter.data_version == delegate.data_version
    assert adapter.query("SKU-001").found is True
    assert slept == []

    controller.activate("inventory_v20_normal")
    assert adapter.data_version == "v2.0"
    assert adapter.query("SKU-001").found is True
    assert slept == [0.15]

    controller.activate("inventory_v21_latency")
    assert adapter.data_version == "v2.1"
    assert adapter.query("SKU-001").found is True
    assert slept == [0.15, 2.3]

    controller.deactivate("inventory_v21_latency")
    assert adapter.data_version == delegate.data_version
    assert adapter.query("SKU-001").found is True
    assert slept == [0.15, 2.3]


def test_forced_timeout_is_removed_when_context_exits() -> None:
    controller = FaultController.from_yaml()
    adapter = FaultInjectingInventoryAdapter(InventoryAdapter(), controller)

    with pytest.raises(RuntimeError):
        with controller.activated("inventory_forced_timeout"):
            with pytest.raises(TimeoutError, match="inventory_forced_timeout"):
                adapter.query("SKU-001")
            raise RuntimeError("test cleanup")

    assert controller.active_scenario(FaultTarget.INVENTORY_ADAPTER) is None
    assert adapter.query("SKU-001").found is True


def test_random_errors_repeat_after_reactivation_with_fixed_seed() -> None:
    controller = FaultController.from_yaml()

    def sample_sequence() -> list[str]:
        outcomes: list[str] = []
        with controller.activated("inventory_random_error"):
            for _ in range(8):
                try:
                    controller.invoke(FaultTarget.INVENTORY_ADAPTER, lambda: "ok")
                except InjectedFaultError:
                    outcomes.append("error")
                else:
                    outcomes.append("ok")
        return outcomes

    first = sample_sequence()
    second = sample_sequence()

    assert first == second
    assert set(first) == {"ok", "error"}


def test_agent_invalid_output_is_injected_without_calling_provider() -> None:
    controller = FaultController.from_yaml()
    delegate = MockModelProvider()
    provider = FaultInjectingModelProvider(delegate, controller)

    with controller.activated("inventory_agent_invalid_output"):
        result = provider.generate(
            [{"role": "user", "content": "{}"}],
            task_type=ModelTaskType.INVENTORY_AGENT.value,
        )

    assert result == "{fault-injected-invalid-json"
    assert delegate.calls == []
    assert controller.active_scenario(FaultTarget.INVENTORY_AGENT) is None


def test_invalid_output_rejects_adapter_target() -> None:
    with pytest.raises(ValueError, match="only supported for Agent"):
        FaultScenario(
            target=FaultTarget.INVENTORY_ADAPTER,
            component_version="v-test",
            invalid_output=True,
        )


def test_unknown_scenario_is_rejected() -> None:
    controller = FaultController.from_yaml()

    with pytest.raises(KeyError, match="Unknown fault scenario"):
        controller.activate("not-configured")
