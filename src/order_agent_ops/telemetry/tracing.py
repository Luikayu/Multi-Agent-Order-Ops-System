"""Lightweight local trace/span recording."""

import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from enum import StrEnum
from threading import RLock
from uuid import uuid4

from pydantic import AwareDatetime, Field

from order_agent_ops.domain.base import DomainModel, NonEmptyString, TraceId
from order_agent_ops.telemetry.metrics import MetricRegistry


class SpanStatus(StrEnum):
    OK = "ok"
    ERROR = "error"


class SpanHandle(DomainModel):
    trace_id: TraceId
    span_id: NonEmptyString
    parent_span_id: str | None = None


class SpanRecord(DomainModel):
    trace_id: TraceId
    span_id: NonEmptyString
    parent_span_id: str | None = None
    component: NonEmptyString
    component_version: str | None = None
    action: NonEmptyString
    started_at: AwareDatetime
    ended_at: AwareDatetime
    duration_ms: float = Field(ge=0)
    status: SpanStatus
    exception_type: str | None = None


class TraceRecorder:
    """Record completed spans and preserve nesting through context variables."""

    def __init__(
        self,
        *,
        metrics: MetricRegistry | None = None,
        logger: logging.Logger | None = None,
        log_store=None,
        component_version_resolver: Callable[[str], str | None] | None = None,
    ) -> None:
        self._spans: list[SpanRecord] = []
        self._lock = RLock()
        self._span_start_order: dict[str, int] = {}
        self._next_start_order = 0
        self._active_spans: ContextVar[tuple[SpanHandle, ...]] = ContextVar(
            f"active_spans_{id(self)}", default=()
        )
        self._metrics = metrics
        self._logger = logger
        self._log_store = log_store
        self._component_version_resolver = component_version_resolver

    @staticmethod
    def new_trace_id() -> str:
        return f"TRACE-{uuid4().hex}"

    @contextmanager
    def span(
        self,
        trace_id: str,
        component: str,
        action: str,
    ) -> Iterator[SpanHandle]:
        component = component.strip()
        action = action.strip()
        if not component:
            raise ValueError("Span component must not be empty")
        if not action:
            raise ValueError("Span action must not be empty")

        stack = self._active_spans.get()
        parent = stack[-1] if stack and stack[-1].trace_id == trace_id else None
        handle = SpanHandle(
            trace_id=trace_id,
            span_id=f"SPAN-{uuid4().hex}",
            parent_span_id=None if parent is None else parent.span_id,
        )
        with self._lock:
            self._span_start_order[handle.span_id] = self._next_start_order
            self._next_start_order += 1
        token = self._active_spans.set((*stack, handle))
        started_at = datetime.now(timezone.utc)
        started_tick = time.perf_counter()
        component_version = (
            self._component_version_resolver(component)
            if self._component_version_resolver is not None
            else None
        )
        status = SpanStatus.OK
        exception_type: str | None = None

        try:
            yield handle
        except Exception as error:
            status = SpanStatus.ERROR
            exception_type = type(error).__name__
            raise
        finally:
            ended_at = datetime.now(timezone.utc)
            duration_ms = max(0.0, (time.perf_counter() - started_tick) * 1000)
            record = SpanRecord(
                trace_id=handle.trace_id,
                span_id=handle.span_id,
                parent_span_id=handle.parent_span_id,
                component=component,
                component_version=component_version,
                action=action,
                started_at=started_at,
                ended_at=ended_at,
                duration_ms=duration_ms,
                status=status,
                exception_type=exception_type,
            )
            with self._lock:
                self._spans.append(record)
            self._active_spans.reset(token)

            if self._log_store is not None:
                self._log_store.record_span(record)

            if self._metrics is not None:
                self._metrics.record(
                    f"{component}.{action}",
                    duration_ms,
                    success=status is SpanStatus.OK,
                )
            if self._logger is not None:
                self._logger.info(
                    "span.completed",
                    extra={
                        "trace_id": record.trace_id,
                        "span_id": record.span_id,
                        "parent_span_id": record.parent_span_id,
                        "component": record.component,
                        "action": record.action,
                        "status": record.status.value,
                        "duration_ms": record.duration_ms,
                        "exception_type": record.exception_type,
                    },
                )

    def get_trace(self, trace_id: str) -> list[SpanRecord]:
        with self._lock:
            matching = [span for span in self._spans if span.trace_id == trace_id]
            start_order = dict(self._span_start_order)
        return sorted(matching, key=lambda span: start_order[span.span_id])

    def list_spans(self) -> list[SpanRecord]:
        with self._lock:
            return list(self._spans)

    def clear(self) -> None:
        with self._lock:
            self._spans.clear()
            self._span_start_order.clear()
            self._next_start_order = 0
