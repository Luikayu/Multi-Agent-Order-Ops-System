"""Optional smoke test for a real local or cloud OpenAI-compatible endpoint."""

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict

from order_agent_ops.main import create_app
from order_agent_ops.models import ModelGateway, OpenAICompatibleProvider


class RemoteSmokeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str


def test_application_selects_openai_compatible_provider_from_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("MODEL_PROVIDER", "openai_compatible")
    monkeypatch.setenv("MODEL_BASE_URL", "http://localhost:8000/v1")
    monkeypatch.setenv("MODEL_API_KEY_ENV", "")
    monkeypatch.setenv("MODEL_NAME", "local-qwen")
    monkeypatch.setenv("MODEL_TIMEOUT_SECONDS", "3")
    application = create_app(database_path=tmp_path / "provider-switch.sqlite3")

    with TestClient(application) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert application.state.model_gateway.provider.provider_name == (
            "openai_compatible"
        )
        assert application.state.model_gateway.provider.model_name == "local-qwen"
        assert application.state.model_gateway.timeout_seconds == 3


@pytest.mark.skipif(
    os.getenv("RUN_REMOTE_MODEL_TESTS") != "1",
    reason="Set RUN_REMOTE_MODEL_TESTS=1 to call a configured real model endpoint",
)
def test_real_openai_compatible_endpoint_returns_validated_json() -> None:
    base_url = os.environ["TEST_MODEL_BASE_URL"]
    model_name = os.environ["TEST_MODEL_NAME"]
    api_key = os.getenv("TEST_MODEL_API_KEY")
    provider = OpenAICompatibleProvider(
        base_url=base_url,
        model_name=model_name,
        api_key=api_key,
        timeout_seconds=float(os.getenv("TEST_MODEL_TIMEOUT_SECONDS", "30")),
    )
    with ModelGateway(provider, max_retries=0, timeout_seconds=35) as gateway:
        result = gateway.generate(
            [
                {
                    "role": "system",
                    "content": (
                        'Return JSON only, exactly in the form {"status":"ok"}.'
                    ),
                }
            ],
            task_type="remote_smoke",
            response_schema=RemoteSmokeResult,
            prompt_version="remote-smoke-v1",
            trace_id="TRACE-REMOTE-MODEL-SMOKE",
        )

    assert result.status == "ok"


@pytest.mark.skipif(
    os.getenv("RUN_OLLAMA_E2E_TESTS") != "1",
    reason="Set RUN_OLLAMA_E2E_TESTS=1 to run the local Ollama shopping flow",
)
def test_real_ollama_completes_natural_language_shopping_flow(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base_url = os.getenv("OLLAMA_TEST_BASE_URL", "http://localhost:11434/v1")
    model_name = os.getenv("OLLAMA_TEST_MODEL", "qwen3:1.7b")
    timeout_seconds = float(os.getenv("OLLAMA_TEST_TIMEOUT_SECONDS", "180"))
    monkeypatch.setenv("MODEL_TIMEOUT_SECONDS", str(timeout_seconds))
    provider = OpenAICompatibleProvider(
        base_url=base_url,
        model_name=model_name,
        timeout_seconds=timeout_seconds,
    )
    application = create_app(
        database_path=tmp_path / "ollama-shopping.sqlite3",
        provider=provider,
    )

    with TestClient(application) as client:
        intent_response = client.post(
            "/shopping/intents",
            json={
                "user_id": "USER-001",
                "message": "我想买一个1000元以内、适合晚上在宿舍写代码的无线机械键盘",
            },
        )
        assert intent_response.status_code == 200, intent_response.text
        intent = intent_response.json()
        assert intent["status"] == "READY_FOR_CONFIRMATION"
        assert intent["parsed_intent"]["ready_to_search"] is True
        assert intent["candidates"]
        recommended_sku = intent["recommendation"]["recommended_sku"]
        assert recommended_sku in {item["sku"] for item in intent["candidates"]}

        confirmation = client.post(
            f"/shopping/intents/{intent['intent_id']}/confirm",
            json={
                "sku": recommended_sku,
                "quantity": 1,
                "idempotency_key": "ollama-e2e-confirmation",
            },
        )

    assert confirmation.status_code == 200, confirmation.text
    assert confirmation.json()["order"]["status"] == "COMPLETED"
