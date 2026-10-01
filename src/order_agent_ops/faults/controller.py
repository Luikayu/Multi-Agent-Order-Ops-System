"""Configuration-driven fault injection without changing business agents."""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from order_agent_ops.business.inventory_adapter import (
    InventoryAdapter,
    InventoryLookupResult,
    ProductSnapshot,
)
from order_agent_ops.business.risk_profile_adapter import (
    RiskProfileAdapter,
    RiskProfileLookupResult,
    UserRiskProfile,
)
from order_agent_ops.models.provider import (
    ModelMessage,
    ModelProvider,
    ModelTaskType,
)


DEFAULT_FAULT_SCENARIOS_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "fault_scenarios.yaml"
)

T = TypeVar("T")


class FaultTarget(StrEnum):
    """Supported injection boundaries."""

    INVENTORY_ADAPTER = "inventory_adapter"
    RISK_ADAPTER = "risk_adapter"
    ORDER_COORDINATOR_AGENT = "order_coordinator_agent"
    INVENTORY_AGENT = "inventory_agent"
    RISK_AGENT = "risk_agent"
    SHOPPING_ASSISTANT_AGENT = "shopping_assistant_agent"
    OPS_GUARDIAN_AGENT = "ops_guardian_agent"


class FaultScenario(BaseModel):
    """One reusable fault profile."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target: FaultTarget
    component_version: str = Field(min_length=1)
    delay_ms: int = Field(default=0, ge=0)
    error_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    force_timeout: bool = False
    invalid_output: bool = False
    seed: int = 0

    @model_validator(mode="after")
    def validate_invalid_output_target(self) -> FaultScenario:
        if self.invalid_output and not self.target.value.endswith("_agent"):
            raise ValueError("invalid_output is only supported for Agent targets")
        return self


class FaultScenarioConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenarios: dict[str, FaultScenario]


class InjectedFaultError(RuntimeError):
    """Raised when a configured random error is injected."""


class FaultController:
    """Activates named fault scenarios and applies them at component boundaries."""

    def __init__(
        self,
        scenarios: Mapping[str, FaultScenario],
        *,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._scenarios = dict(scenarios)
        self._sleeper = sleeper
        self._active: dict[FaultTarget, str] = {}
        self._randoms: dict[str, random.Random] = {}
        self._lock = threading.RLock()

    @classmethod
    def from_yaml(
        cls,
        path: str | Path = DEFAULT_FAULT_SCENARIOS_PATH,
        *,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> FaultController:
        with Path(path).open("r", encoding="utf-8") as stream:
            raw_config = yaml.safe_load(stream) or {}
        config = FaultScenarioConfig.model_validate(raw_config)
        return cls(config.scenarios, sleeper=sleeper)

    @property
    def scenarios(self) -> Mapping[str, FaultScenario]:
        return dict(self._scenarios)

    def activate(self, scenario_name: str) -> FaultScenario:
        try:
            scenario = self._scenarios[scenario_name]
        except KeyError as exc:
            raise KeyError(f"Unknown fault scenario: {scenario_name}") from exc
        with self._lock:
            previous_name = self._active.get(scenario.target)
            if previous_name is not None and previous_name != scenario_name:
                self._randoms.pop(previous_name, None)
            self._active[scenario.target] = scenario_name
            self._randoms[scenario_name] = random.Random(scenario.seed)
        return scenario

    def deactivate(self, scenario_name: str) -> None:
        scenario = self._require_scenario(scenario_name)
        with self._lock:
            if self._active.get(scenario.target) == scenario_name:
                self._active.pop(scenario.target, None)
            self._randoms.pop(scenario_name, None)

    def clear(self) -> None:
        with self._lock:
            self._active.clear()
            self._randoms.clear()

    def deactivate_target(self, target: FaultTarget | str) -> None:
        normalized_target = FaultTarget(target)
        with self._lock:
            scenario_name = self._active.pop(normalized_target, None)
            if scenario_name is not None:
                self._randoms.pop(scenario_name, None)

    def active_scenario(
        self, target: FaultTarget | str
    ) -> FaultScenario | None:
        normalized_target = FaultTarget(target)
        with self._lock:
            scenario_name = self._active.get(normalized_target)
            return self._scenarios.get(scenario_name) if scenario_name else None

    def active_scenario_name(self, target: FaultTarget | str) -> str | None:
        with self._lock:
            return self._active.get(FaultTarget(target))

    @contextmanager
    def activated(self, scenario_name: str) -> Iterator[FaultScenario]:
        scenario = self._require_scenario(scenario_name)
        with self._lock:
            previous_name = self._active.get(scenario.target)
            previous_state = None
            if previous_name is not None and previous_name in self._randoms:
                previous_state = self._randoms[previous_name].getstate()
        self.activate(scenario_name)
        try:
            yield scenario
        finally:
            with self._lock:
                self._active.pop(scenario.target, None)
                self._randoms.pop(scenario_name, None)
                if previous_name is not None:
                    self._active[scenario.target] = previous_name
                    previous = self._scenarios[previous_name]
                    restored_random = random.Random(previous.seed)
                    if previous_state is not None:
                        restored_random.setstate(previous_state)
                    self._randoms[previous_name] = restored_random

    def invoke(
        self,
        target: FaultTarget | str,
        operation: Callable[..., T],
        *args: Any,
        **kwargs: Any,
    ) -> T:
        self._apply_before_call(FaultTarget(target))
        return operation(*args, **kwargs)

    def invoke_model(
        self,
        target: FaultTarget | str,
        operation: Callable[[], str],
    ) -> str:
        scenario = self._apply_before_call(FaultTarget(target))
        if scenario is not None and scenario.invalid_output:
            return "{fault-injected-invalid-json"
        return operation()

    def _apply_before_call(self, target: FaultTarget) -> FaultScenario | None:
        with self._lock:
            scenario_name = self._active.get(target)
            if scenario_name is None:
                return None
            scenario = self._scenarios[scenario_name]
            inject_random_error = False
            if scenario.error_rate > 0:
                randomizer = self._randoms.setdefault(
                    scenario_name, random.Random(scenario.seed)
                )
                inject_random_error = randomizer.random() < scenario.error_rate

        if scenario.delay_ms:
            self._sleeper(scenario.delay_ms / 1000)
        if scenario.force_timeout:
            raise TimeoutError(f"Injected timeout: {scenario_name}")
        if inject_random_error:
            raise InjectedFaultError(f"Injected random error: {scenario_name}")
        return scenario

    def _require_scenario(self, scenario_name: str) -> FaultScenario:
        try:
            return self._scenarios[scenario_name]
        except KeyError as exc:
            raise KeyError(f"Unknown fault scenario: {scenario_name}") from exc


class FaultInjectingInventoryAdapter:
    """Inventory adapter decorator controlled by ``FaultController``."""

    def __init__(
        self, delegate: InventoryAdapter, controller: FaultController
    ) -> None:
        self._delegate = delegate
        self._controller = controller

    @property
    def path(self) -> Path:
        return self._delegate.path

    @property
    def data_version(self) -> str:
        scenario = self._controller.active_scenario(FaultTarget.INVENTORY_ADAPTER)
        return scenario.component_version if scenario else self._delegate.data_version

    def query(self, sku: str) -> InventoryLookupResult:
        return self._controller.invoke(
            FaultTarget.INVENTORY_ADAPTER, self._delegate.query, sku
        )

    def list_products(self) -> list[ProductSnapshot]:
        return self._delegate.list_products()


class FaultInjectingRiskProfileAdapter:
    """Risk-profile adapter decorator controlled by ``FaultController``."""

    def __init__(
        self, delegate: RiskProfileAdapter, controller: FaultController
    ) -> None:
        self._delegate = delegate
        self._controller = controller

    @property
    def path(self) -> Path:
        return self._delegate.path

    @property
    def data_version(self) -> str:
        scenario = self._controller.active_scenario(FaultTarget.RISK_ADAPTER)
        return scenario.component_version if scenario else self._delegate.data_version

    def query(self, user_id: str) -> RiskProfileLookupResult:
        return self._controller.invoke(
            FaultTarget.RISK_ADAPTER, self._delegate.query, user_id
        )

    def list_profiles(self) -> list[UserRiskProfile]:
        return self._delegate.list_profiles()


class FaultInjectingModelProvider(ModelProvider):
    """Model-provider decorator that targets logical Agent calls."""

    _TARGET_BY_TASK: dict[ModelTaskType, FaultTarget] = {
        ModelTaskType.ORDER_COORDINATOR: FaultTarget.ORDER_COORDINATOR_AGENT,
        ModelTaskType.INVENTORY_AGENT: FaultTarget.INVENTORY_AGENT,
        ModelTaskType.RISK_AGENT: FaultTarget.RISK_AGENT,
        ModelTaskType.SHOPPING_ANALYZE: FaultTarget.SHOPPING_ASSISTANT_AGENT,
        ModelTaskType.SHOPPING_CATALOG_SEARCH: FaultTarget.SHOPPING_ASSISTANT_AGENT,
        ModelTaskType.SHOPPING_RANK: FaultTarget.SHOPPING_ASSISTANT_AGENT,
        ModelTaskType.OPS_GUARDIAN: FaultTarget.OPS_GUARDIAN_AGENT,
    }

    def __init__(self, delegate: ModelProvider, controller: FaultController) -> None:
        self._delegate = delegate
        self._controller = controller

    @property
    def provider_name(self) -> str:
        return self._delegate.provider_name

    @property
    def model_name(self) -> str:
        return self._delegate.model_name

    def generate(
        self,
        messages: Sequence[ModelMessage],
        *,
        task_type: str,
        response_schema: type[BaseModel] | None = None,
    ) -> str:
        task = ModelTaskType(task_type)
        target = self._TARGET_BY_TASK[task]
        return self._controller.invoke_model(
            target,
            lambda: self._delegate.generate(
                messages,
                task_type=task_type,
                response_schema=response_schema,
            ),
        )

    def close(self) -> None:
        self._delegate.close()
