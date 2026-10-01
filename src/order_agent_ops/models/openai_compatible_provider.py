"""Minimal synchronous client for OpenAI-compatible Chat Completions APIs."""

from __future__ import annotations

from collections.abc import Sequence
import json
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel

from order_agent_ops.models.provider import ModelMessage, ModelProvider


class OpenAICompatibleProviderError(RuntimeError):
    """Raised when an endpoint returns an unusable Chat Completions response."""


class OpenAICompatibleProvider(ModelProvider):
    """Call the lowest-common-denominator ``/chat/completions`` endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        model_name: str,
        api_key: str | None = None,
        timeout_seconds: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        normalized_base_url = base_url.strip().rstrip("/")
        parsed_url = urlparse(normalized_base_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError("Model base_url must be an absolute HTTP(S) URL")
        if not model_name.strip():
            raise ValueError("Model name must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("Model provider timeout must be greater than zero")

        self._base_url = normalized_base_url
        self._model_name = model_name.strip()
        self._api_key = api_key.strip() if api_key and api_key.strip() else None
        self._client = httpx.Client(
            timeout=timeout_seconds,
            transport=transport,
        )

    @property
    def provider_name(self) -> str:
        return "openai_compatible"

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def endpoint(self) -> str:
        return f"{self._base_url}/chat/completions"

    def generate(
        self,
        messages: Sequence[ModelMessage],
        *,
        task_type: str,
        response_schema: type[BaseModel] | None = None,
    ) -> str:
        del task_type
        prepared_messages = [dict(message) for message in messages]
        payload: dict[str, object] = {
            "model": self._model_name,
            "messages": prepared_messages,
        }
        if response_schema is not None:
            json_schema = response_schema.model_json_schema()
            schema_instruction = (
                "Return exactly one JSON object matching the following JSON Schema. "
                "Use the exact property names and value types, include every required "
                "property, and do not add properties. The application will reject any "
                "response that violates the schema or its semantic rules. JSON Schema: "
                + json.dumps(json_schema, ensure_ascii=False, separators=(",", ":"))
            )
            if prepared_messages and prepared_messages[0].get("role") == "system":
                prepared_messages[0]["content"] = (
                    prepared_messages[0]["content"] + "\n\n" + schema_instruction
                )
            else:
                prepared_messages.insert(
                    0,
                    {"role": "system", "content": schema_instruction},
                )
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": response_schema.__name__,
                    "strict": True,
                    "schema": json_schema,
                },
            }

        headers = {"Content-Type": "application/json"}
        if self._api_key is not None:
            headers["Authorization"] = f"Bearer {self._api_key}"

        try:
            response = self._post(payload, headers=headers)
            if (
                response_schema is not None
                and response.status_code in {400, 404, 415, 422}
            ):
                # Some otherwise compatible servers implement JSON object mode but
                # not the newer JSON Schema response format. Fall back once; the
                # schema remains in the system message and ModelGateway still
                # performs the authoritative Pydantic validation.
                fallback_payload = dict(payload)
                fallback_payload["response_format"] = {"type": "json_object"}
                response = self._post(fallback_payload, headers=headers)
            response.raise_for_status()
        except httpx.TimeoutException as error:
            raise TimeoutError("OpenAI-compatible endpoint timed out") from error
        except httpx.HTTPStatusError as error:
            raise OpenAICompatibleProviderError(
                "OpenAI-compatible endpoint returned HTTP "
                f"{error.response.status_code}"
            ) from error
        except httpx.HTTPError as error:
            raise OpenAICompatibleProviderError(
                "OpenAI-compatible endpoint request failed"
            ) from error

        try:
            body = response.json()
            choices = body["choices"]
            content = choices[0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise OpenAICompatibleProviderError(
                "OpenAI-compatible response is missing choices[0].message.content"
            ) from error
        if not isinstance(content, str) or not content.strip():
            raise OpenAICompatibleProviderError(
                "OpenAI-compatible response content must be a non-empty string"
            )
        return content

    def _post(
        self,
        payload: dict[str, object],
        *,
        headers: dict[str, str],
    ) -> httpx.Response:
        return self._client.post(
            self.endpoint,
            json=payload,
            headers=headers,
        )

    def close(self) -> None:
        self._client.close()
