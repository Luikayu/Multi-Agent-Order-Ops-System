"""Shared agent identity, tracing, and run-record behavior."""

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock
from typing import ClassVar
from uuid import uuid4

from pydantic import BaseModel

from order_agent_ops.domain.agents import AgentRunRecord
from order_agent_ops.domain.enums import RunStatus
from order_agent_ops.models.errors import (
    InvalidModelResponseError,
    ModelResponseValidationError,
    ModelTimeoutError,
)
from order_agent_ops.models.gateway import ModelGateway
from order_agent_ops.telemetry.tracing import TraceRecorder


@dataclass(frozen=True, slots=True)
class AgentRunContext:
    run_id: str


class BaseAgent:
    """Record every agent run without coupling agents to a provider SDK."""

    name: ClassVar[str]
    version: ClassVar[str]
    prompt_version: ClassVar[str]
    output_schema: ClassVar[type[BaseModel]]

    def __init__(
        self,
        model_gateway: ModelGateway,
        *,
        trace_recorder: TraceRecorder | None = None,
    ) -> None:
        self.model_gateway = model_gateway
        self.trace_recorder = trace_recorder or model_gateway.trace_recorder
        self._run_records: list[AgentRunRecord] = []
        self._run_lock = RLock()

    @staticmethod
    def _status_for_error(error: Exception) -> RunStatus:
        if isinstance(error, ModelTimeoutError) or isinstance(error, TimeoutError):
            return RunStatus.TIMEOUT
        if isinstance(
            error,
            (InvalidModelResponseError, ModelResponseValidationError),
        ):
            return RunStatus.INVALID_OUTPUT
        return RunStatus.ERROR

    @contextmanager
    def track_run(
        self,
        trace_id: str,
        *,
        action: str,
        model_task_type: str,
        prompt_version: str | None = None,
    ) -> Iterator[AgentRunContext]:
        active_prompt_version = prompt_version or self.prompt_version
        run_context = AgentRunContext(run_id=f"RUN-{uuid4().hex}")
        started_at = datetime.now(timezone.utc)
        started_tick = time.perf_counter()
        status = RunStatus.SUCCESS
        error_type: str | None = None

        try:
            with self.trace_recorder.span(trace_id, self.name, action):
                yield run_context
        except Exception as error:
            status = self._status_for_error(error)
            error_type = type(error).__name__
            raise
        finally:
            ended_at = datetime.now(timezone.utc)
            duration_ms = max(0.0, (time.perf_counter() - started_tick) * 1000)
            model_calls = [
                record
                for record in self.model_gateway.list_calls(trace_id)
                if record.agent_run_id == run_context.run_id
                and record.task_type == model_task_type
                and record.prompt_version == active_prompt_version
            ]
            retry_count = max(
                (record.retry_count for record in model_calls),
                default=0,
            )
            run_record = AgentRunRecord(
                run_id=run_context.run_id,
                trace_id=trace_id,
                agent_name=self.name,
                agent_version=self.version,
                prompt_version=active_prompt_version,
                model_provider=self.model_gateway.provider.provider_name,
                model_name=self.model_gateway.provider.model_name,
                status=status,
                started_at=started_at,
                ended_at=ended_at,
                duration_ms=duration_ms,
                retry_count=retry_count,
                error_type=error_type,
            )
            with self._run_lock:
                self._run_records.append(run_record)

    def list_run_records(self, trace_id: str | None = None) -> list[AgentRunRecord]:
        with self._run_lock:
            records = list(self._run_records)
        if trace_id is None:
            return records
        return [record for record in records if record.trace_id == trace_id]
