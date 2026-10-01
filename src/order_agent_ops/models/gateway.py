"""Timeout, retry, validation, and telemetry around a model provider."""

import json
import logging
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime, timezone
from enum import StrEnum
from threading import RLock
from typing import TypeVar
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, Field, ValidationError

from order_agent_ops.domain.base import DomainModel, NonEmptyString, RunId, TraceId
from order_agent_ops.models.errors import (
    InvalidModelResponseError,
    ModelGatewayError,
    ModelResponseValidationError,
    ModelTimeoutError,
    ProviderInvocationError,
)
from order_agent_ops.models.provider import ModelProvider
from order_agent_ops.telemetry.metrics import MetricRegistry
from order_agent_ops.telemetry.tracing import TraceRecorder


SchemaT = TypeVar("SchemaT", bound=BaseModel)


class ModelCallStatus(StrEnum):
    SUCCESS = "success"
    TIMEOUT = "timeout"
    INVALID_OUTPUT = "invalid_output"
    ERROR = "error"


class ModelCallRecord(DomainModel):
    call_id: NonEmptyString
    trace_id: TraceId
    task_type: NonEmptyString
    provider: NonEmptyString
    model_name: NonEmptyString
    prompt_version: NonEmptyString
    agent_run_id: RunId | None = None
    attempt: int = Field(ge=1)
    retry_count: int = Field(ge=0)
    started_at: AwareDatetime
    ended_at: AwareDatetime
    duration_ms: float = Field(ge=0)
    status: ModelCallStatus
    error_type: str | None = None


class ModelGateway:
    """Run one initial model attempt and at most one retry."""

    def __init__(
        self,
        provider: ModelProvider,
        *,
        timeout_seconds: float = 10.0,
        max_retries: int = 1,
        trace_recorder: TraceRecorder | None = None,
        metrics: MetricRegistry | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Model timeout must be greater than zero")
        if max_retries not in {0, 1}:
            raise ValueError("Model gateway supports zero or one retry")

        self.provider = provider
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.trace_recorder = trace_recorder or TraceRecorder(logger=logger)
        self.metrics = metrics or MetricRegistry()
        self._logger = logger
        self._executor = ThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix="model-gateway",
        )
        self._records: list[ModelCallRecord] = []
        self._lock = RLock()

    def __enter__(self) -> "ModelGateway":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self, *, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=True)
        self.provider.close()

    def _invoke_provider(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        task_type: str,
        response_schema: type[BaseModel],
    ) -> str:
        future = self._executor.submit(
            self.provider.generate,
            messages,
            task_type=task_type,
            response_schema=response_schema,
        )
        try:
            return future.result(timeout=self.timeout_seconds)
        except FutureTimeoutError as error:
            future.cancel()
            raise ModelTimeoutError(
                f"Model provider timed out after {self.timeout_seconds} seconds"
            ) from error
        except Exception as error:
            raise ProviderInvocationError(
                f"Provider {self.provider.provider_name} failed with "
                f"{type(error).__name__}"
            ) from error

    @staticmethod
    def _validate_response(raw_response: str, response_schema: type[SchemaT]) -> SchemaT:
        try:
            payload = json.loads(raw_response)
        except (json.JSONDecodeError, TypeError) as error:
            raise InvalidModelResponseError(
                "Model response is not valid JSON"
            ) from error
        if not isinstance(payload, dict):
            raise InvalidModelResponseError("Model response must be a JSON object")
        try:
            return response_schema.model_validate(payload)
        except ValidationError as error:
            raise ModelResponseValidationError(
                f"Model response failed {response_schema.__name__} validation"
            ) from error

    @staticmethod
    def _status_for_error(error: ModelGatewayError) -> ModelCallStatus:
        if isinstance(error, ModelTimeoutError):
            return ModelCallStatus.TIMEOUT
        if isinstance(
            error,
            (InvalidModelResponseError, ModelResponseValidationError),
        ):
            return ModelCallStatus.INVALID_OUTPUT
        return ModelCallStatus.ERROR

    def _record_attempt(
        self,
        *,
        trace_id: str,
        task_type: str,
        prompt_version: str,
        agent_run_id: str | None,
        attempt: int,
        started_at: datetime,
        ended_at: datetime,
        duration_ms: float,
        status: ModelCallStatus,
        error_type: str | None,
    ) -> ModelCallRecord:
        record = ModelCallRecord(
            call_id=f"MODEL-CALL-{uuid4().hex}",
            trace_id=trace_id,
            task_type=task_type,
            provider=self.provider.provider_name,
            model_name=self.provider.model_name,
            prompt_version=prompt_version,
            agent_run_id=agent_run_id,
            attempt=attempt,
            retry_count=attempt - 1,
            started_at=started_at,
            ended_at=ended_at,
            duration_ms=duration_ms,
            status=status,
            error_type=error_type,
        )
        with self._lock:
            self._records.append(record)

        self.metrics.record(
            f"model.{self.provider.provider_name}.{task_type}",
            duration_ms,
            success=status is ModelCallStatus.SUCCESS,
        )
        if self._logger is not None:
            self._logger.info(
                "model.call.completed",
                extra={
                    "trace_id": trace_id,
                    "component": "model-gateway",
                    "action": task_type,
                    "status": status.value,
                    "duration_ms": duration_ms,
                    "exception_type": error_type,
                    "context": {
                        "call_id": record.call_id,
                        "provider": record.provider,
                        "model_name": record.model_name,
                        "prompt_version": record.prompt_version,
                        "agent_run_id": record.agent_run_id,
                        "attempt": attempt,
                    },
                },
            )
        return record

    def generate(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        task_type: str,
        response_schema: type[SchemaT],
        prompt_version: str,
        trace_id: str,
        agent_run_id: str | None = None,
    ) -> SchemaT:
        task_type = task_type.strip()
        prompt_version = prompt_version.strip()
        if not task_type:
            raise ValueError("Model task type must not be empty")
        if not prompt_version:
            raise ValueError("Prompt version must not be empty")

        safe_messages = tuple(dict(message) for message in messages)
        for attempt in range(1, self.max_retries + 2):
            started_at = datetime.now(timezone.utc)
            started_tick = time.perf_counter()
            try:
                with self.trace_recorder.span(
                    trace_id,
                    "model-gateway",
                    f"generate.{task_type}",
                ):
                    raw_response = self._invoke_provider(
                        safe_messages,
                        task_type=task_type,
                        response_schema=response_schema,
                    )
                    result = self._validate_response(raw_response, response_schema)
            except ModelGatewayError as error:
                ended_at = datetime.now(timezone.utc)
                duration_ms = max(0.0, (time.perf_counter() - started_tick) * 1000)
                self._record_attempt(
                    trace_id=trace_id,
                    task_type=task_type,
                    prompt_version=prompt_version,
                    agent_run_id=agent_run_id,
                    attempt=attempt,
                    started_at=started_at,
                    ended_at=ended_at,
                    duration_ms=duration_ms,
                    status=self._status_for_error(error),
                    error_type=type(error).__name__,
                )
                if attempt > self.max_retries:
                    raise
            else:
                ended_at = datetime.now(timezone.utc)
                duration_ms = max(0.0, (time.perf_counter() - started_tick) * 1000)
                self._record_attempt(
                    trace_id=trace_id,
                    task_type=task_type,
                    prompt_version=prompt_version,
                    agent_run_id=agent_run_id,
                    attempt=attempt,
                    started_at=started_at,
                    ended_at=ended_at,
                    duration_ms=duration_ms,
                    status=ModelCallStatus.SUCCESS,
                    error_type=None,
                )
                return result

        raise AssertionError("Model gateway exhausted attempts without returning or raising")

    def list_calls(self, trace_id: str | None = None) -> list[ModelCallRecord]:
        with self._lock:
            records = list(self._records)
        if trace_id is None:
            return records
        return [record for record in records if record.trace_id == trace_id]
