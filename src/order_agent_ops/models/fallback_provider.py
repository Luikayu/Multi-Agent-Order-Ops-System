"""Observable failover between two model providers."""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence

from pydantic import BaseModel

from order_agent_ops.models.provider import ModelMessage, ModelProvider
from order_agent_ops.telemetry.metrics import MetricRegistry


class FallbackModelProvider(ModelProvider):
    """Use the secondary provider only when primary invocation raises."""

    def __init__(
        self,
        primary: ModelProvider,
        fallback: ModelProvider,
        *,
        metrics: MetricRegistry | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        if primary is fallback:
            raise ValueError("Primary and fallback providers must be different")
        self.primary = primary
        self.fallback = fallback
        self.metrics = metrics or MetricRegistry()
        self._logger = logger or logging.getLogger("order_agent_ops.models.fallback")

    @property
    def provider_name(self) -> str:
        return "fallback"

    @property
    def model_name(self) -> str:
        return f"{self.primary.model_name}->{self.fallback.model_name}"

    def generate(
        self,
        messages: Sequence[ModelMessage],
        *,
        task_type: str,
        response_schema: type[BaseModel] | None = None,
    ) -> str:
        try:
            return self.primary.generate(
                messages,
                task_type=task_type,
                response_schema=response_schema,
            )
        except Exception as primary_error:
            self.metrics.record(
                "model.fallback.activation",
                0,
                success=False,
            )
            self._logger.warning(
                "model.provider.fallback",
                extra={
                    "component": "model-provider",
                    "action": task_type,
                    "status": "degraded",
                    "exception_type": type(primary_error).__name__,
                    "context": {
                        "primary_provider": self.primary.provider_name,
                        "primary_model": self.primary.model_name,
                        "fallback_provider": self.fallback.provider_name,
                        "fallback_model": self.fallback.model_name,
                    },
                },
            )
            started = time.perf_counter()
            try:
                result = self.fallback.generate(
                    messages,
                    task_type=task_type,
                    response_schema=response_schema,
                )
            except Exception:
                self.metrics.record(
                    "model.fallback.result",
                    max(0.0, (time.perf_counter() - started) * 1000),
                    success=False,
                )
                raise
            self.metrics.record(
                "model.fallback.result",
                max(0.0, (time.perf_counter() - started) * 1000),
                success=True,
            )
            return result

    def close(self) -> None:
        self.primary.close()
        self.fallback.close()
