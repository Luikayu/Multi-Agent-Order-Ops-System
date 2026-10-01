"""Configurable fault injection for demonstrations and tests."""

from order_agent_ops.faults.controller import (
    DEFAULT_FAULT_SCENARIOS_PATH,
    FaultController,
    FaultInjectingInventoryAdapter,
    FaultInjectingModelProvider,
    FaultInjectingRiskProfileAdapter,
    FaultScenario,
    FaultTarget,
    InjectedFaultError,
)

__all__ = [
    "DEFAULT_FAULT_SCENARIOS_PATH",
    "FaultController",
    "FaultInjectingInventoryAdapter",
    "FaultInjectingModelProvider",
    "FaultInjectingRiskProfileAdapter",
    "FaultScenario",
    "FaultTarget",
    "InjectedFaultError",
]
