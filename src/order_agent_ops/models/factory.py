"""Configuration-only model provider selection."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping

from order_agent_ops.config import FallbackModelSettings, ModelSettings
from order_agent_ops.models.fallback_provider import FallbackModelProvider
from order_agent_ops.models.mock_provider import MockModelProvider
from order_agent_ops.models.openai_compatible_provider import (
    OpenAICompatibleProvider,
)
from order_agent_ops.models.provider import ModelProvider
from order_agent_ops.telemetry.metrics import MetricRegistry


class ModelConfigurationError(ValueError):
    """Raised before startup when an endpoint credential is misconfigured."""


def _resolve_api_key(
    environment_name: str | None,
    environment: Mapping[str, str],
) -> str | None:
    if environment_name is None:
        return None
    value = environment.get(environment_name)
    if value is None or not value.strip():
        raise ModelConfigurationError(
            f"Model API key environment variable is not set: {environment_name}"
        )
    return value


def _build_fallback_endpoint(
    settings: FallbackModelSettings,
    environment: Mapping[str, str],
) -> OpenAICompatibleProvider:
    if not settings.base_url or not settings.model_name:
        raise ModelConfigurationError(
            "Enabled model fallback requires base_url and model_name"
        )
    return OpenAICompatibleProvider(
        base_url=settings.base_url,
        model_name=settings.model_name,
        api_key=_resolve_api_key(settings.api_key_env, environment),
        timeout_seconds=settings.timeout_seconds,
    )


def build_model_provider(
    settings: ModelSettings,
    *,
    environment: Mapping[str, str] | None = None,
    metrics: MetricRegistry | None = None,
    logger: logging.Logger | None = None,
) -> ModelProvider:
    """Build Mock, local, or cloud providers without changing Agent code."""

    active_environment = environment if environment is not None else os.environ
    if settings.provider == "mock":
        return MockModelProvider(model_name=settings.model_name)

    primary = OpenAICompatibleProvider(
        base_url=settings.base_url or "",
        model_name=settings.model_name,
        api_key=_resolve_api_key(settings.api_key_env, active_environment),
        timeout_seconds=settings.timeout_seconds,
    )
    if not settings.fallback.enabled:
        return primary
    try:
        fallback = _build_fallback_endpoint(settings.fallback, active_environment)
    except Exception:
        primary.close()
        raise
    return FallbackModelProvider(
        primary,
        fallback,
        metrics=metrics,
        logger=logger,
    )
