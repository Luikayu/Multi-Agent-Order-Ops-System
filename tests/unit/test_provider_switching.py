import io
import json
import logging

import httpx
import pytest
from pydantic import BaseModel, ConfigDict

from order_agent_ops.agents.shopping_assistant import ShoppingAssistantAgent
from order_agent_ops.config import FallbackModelSettings, ModelSettings
from order_agent_ops.domain.shopping import ParsedPurchaseIntent
from order_agent_ops.models import (
    FallbackModelProvider,
    MockModelProvider,
    ModelConfigurationError,
    ModelGateway,
    OpenAICompatibleProvider,
    OpenAICompatibleProviderError,
    build_model_provider,
)
from order_agent_ops.models.provider import ModelProvider
from order_agent_ops.telemetry.logging import configure_json_logging
from order_agent_ops.telemetry.metrics import MetricRegistry


class SmallResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str


def completion(content: dict[str, object]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-test",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(content),
                    },
                    "finish_reason": "stop",
                }
            ],
        },
    )


def test_openai_compatible_provider_uses_chat_completions_without_leaking_key() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return completion({"answer": "ok"})

    provider = OpenAICompatibleProvider(
        base_url="http://localhost:8000/v1/",
        model_name="qwen-local",
        api_key="test-secret-key",
        transport=httpx.MockTransport(handler),
    )
    try:
        raw = provider.generate(
            [{"role": "system", "content": "Return JSON only."}],
            task_type="test",
            response_schema=SmallResult,
        )
    finally:
        provider.close()

    payload = json.loads(requests[0].content)
    assert requests[0].url == "http://localhost:8000/v1/chat/completions"
    assert requests[0].headers["authorization"] == "Bearer test-secret-key"
    assert payload["model"] == "qwen-local"
    assert payload["messages"][0]["role"] == "system"
    assert payload["messages"][0]["content"].startswith("Return JSON only.")
    assert "JSON Schema" in payload["messages"][0]["content"]
    response_format = payload["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == "SmallResult"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["schema"] == SmallResult.model_json_schema()
    assert "test-secret-key" not in requests[0].content.decode()
    assert json.loads(raw) == {"answer": "ok"}


def test_provider_falls_back_once_when_json_schema_mode_is_unsupported() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content)
        if payload["response_format"]["type"] == "json_schema":
            return httpx.Response(400, json={"error": "unsupported response format"})
        return completion({"answer": "fallback-ok"})

    provider = OpenAICompatibleProvider(
        base_url="http://legacy-compatible.test/v1",
        model_name="legacy-model",
        transport=httpx.MockTransport(handler),
    )
    try:
        raw = provider.generate(
            [{"role": "user", "content": "Return a result"}],
            task_type="test",
            response_schema=SmallResult,
        )
    finally:
        provider.close()

    assert len(requests) == 2
    first = json.loads(requests[0].content)
    second = json.loads(requests[1].content)
    assert first["response_format"]["type"] == "json_schema"
    assert second["response_format"] == {"type": "json_object"}
    assert first["messages"] == second["messages"]
    assert first["messages"][0]["role"] == "system"
    assert "JSON Schema" in first["messages"][0]["content"]
    assert json.loads(raw) == {"answer": "fallback-ok"}


def test_provider_maps_timeout_and_rejects_malformed_response() -> None:
    def timeout_handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("endpoint too slow")

    timeout_provider = OpenAICompatibleProvider(
        base_url="http://local.test/v1",
        model_name="local-model",
        transport=httpx.MockTransport(timeout_handler),
    )
    with pytest.raises(TimeoutError, match="timed out"):
        timeout_provider.generate([], task_type="test")
    timeout_provider.close()

    malformed_provider = OpenAICompatibleProvider(
        base_url="https://cloud.test/v1",
        model_name="cloud-model",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"choices": []})
        ),
    )
    with pytest.raises(OpenAICompatibleProviderError, match="message.content"):
        malformed_provider.generate([], task_type="test")
    malformed_provider.close()


def test_mock_local_and_cloud_selection_uses_configuration_only() -> None:
    mock = build_model_provider(
        ModelSettings(provider="mock", model_name="mock-model")
    )
    local = build_model_provider(
        ModelSettings(
            provider="openai_compatible",
            base_url="http://localhost:8000/v1",
            model_name="qwen-local",
        )
    )
    cloud = build_model_provider(
        ModelSettings(
            provider="openai_compatible",
            base_url="https://cloud.example/v1",
            api_key_env="CLOUD_TEST_KEY",
            model_name="cloud-model",
        ),
        environment={"CLOUD_TEST_KEY": "not-a-real-key"},
    )
    try:
        assert isinstance(mock, MockModelProvider)
        assert isinstance(local, OpenAICompatibleProvider)
        assert isinstance(cloud, OpenAICompatibleProvider)
        assert local.model_name == "qwen-local"
        assert cloud.model_name == "cloud-model"
    finally:
        mock.close()
        local.close()
        cloud.close()


def test_factory_wraps_configured_secondary_endpoint() -> None:
    metrics = MetricRegistry()
    provider = build_model_provider(
        ModelSettings(
            provider="openai_compatible",
            base_url="http://localhost:8000/v1",
            model_name="local-model",
            fallback=FallbackModelSettings(
                enabled=True,
                base_url="https://cloud.example/v1",
                api_key_env="CLOUD_MODEL_KEY",
                model_name="cloud-model",
            ),
        ),
        environment={"CLOUD_MODEL_KEY": "not-a-real-key"},
        metrics=metrics,
    )
    try:
        assert isinstance(provider, FallbackModelProvider)
        assert provider.primary.model_name == "local-model"
        assert provider.fallback.model_name == "cloud-model"
        assert provider.metrics is metrics
    finally:
        provider.close()


def test_configured_api_key_name_must_exist_but_mock_needs_no_key() -> None:
    mock = build_model_provider(
        ModelSettings(provider="mock", model_name="mock-model"),
        environment={},
    )
    mock.close()

    with pytest.raises(ModelConfigurationError, match="CLOUD_MODEL_KEY"):
        build_model_provider(
            ModelSettings(
                provider="openai_compatible",
                base_url="https://cloud.example/v1",
                api_key_env="CLOUD_MODEL_KEY",
                model_name="cloud-model",
            ),
            environment={},
        )


class RaisingProvider(ModelProvider):
    @property
    def provider_name(self) -> str:
        return "primary"

    @property
    def model_name(self) -> str:
        return "primary-model"

    def generate(self, messages, *, task_type, response_schema=None) -> str:
        raise TimeoutError("primary unavailable")


def test_fallback_records_degradation_without_logging_credentials() -> None:
    stream = io.StringIO()
    logger = configure_json_logging(
        logging.getLogger("test.model.fallback"),
        stream=stream,
    )
    metrics = MetricRegistry()
    fallback = MockModelProvider(
        model_name="fallback-model",
        responses={"test": {"answer": "fallback-ok"}},
    )
    provider = FallbackModelProvider(
        RaisingProvider(),
        fallback,
        metrics=metrics,
        logger=logger,
    )

    result = provider.generate(
        [{"role": "user", "content": "Return JSON only"}],
        task_type="test",
        response_schema=SmallResult,
    )
    provider.close()

    assert json.loads(result) == {"answer": "fallback-ok"}
    assert metrics.summary("model.fallback.activation").count == 1
    assert metrics.summary("model.fallback.activation").error_count == 1
    assert metrics.summary("model.fallback.result").success_count == 1
    log_output = stream.getvalue()
    assert "model.provider.fallback" in log_output
    assert "primary-model" in log_output
    assert "api_key" not in log_output.casefold()


def test_shopping_agent_keeps_same_schema_with_openai_compatible_provider() -> None:
    intent_payload = {
        "category": "keyboard",
        "quantity": 1,
        "max_price": "500.00",
        "use_case": "dormitory",
        "constraint_conflicts": [],
        "clarifying_question": None,
    }
    provider = OpenAICompatibleProvider(
        base_url="http://qwen.local/v1",
        model_name="qwen",
        transport=httpx.MockTransport(
            lambda _request: completion(intent_payload)
        ),
    )
    with ModelGateway(provider, max_retries=0) as gateway:
        result = ShoppingAssistantAgent(gateway).analyze(
            ["想买一把宿舍用的安静键盘，预算五百元"],
            trace_id="TRACE-OPENAI-COMPATIBLE-SHOPPING",
        )

    assert isinstance(result, ParsedPurchaseIntent)
    assert result.category == "keyboard"
    assert result.ready_to_search is True
    assert result.inferred_requirements == []


def test_fallback_settings_require_complete_secondary_endpoint() -> None:
    with pytest.raises(ValueError, match="fallback requires"):
        FallbackModelSettings(enabled=True, base_url="http://fallback.test/v1")
