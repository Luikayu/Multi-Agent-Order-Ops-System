"""Thread-safe in-memory store for structured operational log events."""

from __future__ import annotations

from datetime import datetime
from threading import RLock
from uuid import uuid4

from pydantic import AwareDatetime, Field

from order_agent_ops.domain.base import DomainModel, NonEmptyString, TraceId
from order_agent_ops.telemetry.tracing import SpanRecord


class OperationalLogRecord(DomainModel):
    log_id: NonEmptyString
    time: AwareDatetime
    level: NonEmptyString
    message: NonEmptyString
    component: NonEmptyString
    component_version: str | None = None
    action: NonEmptyString
    status: NonEmptyString
    duration_ms: float = Field(ge=0)
    trace_id: TraceId
    span_id: NonEmptyString
    exception_type: str | None = None


class OperationalLogStore:
    """Retain sanitized span-completion facts for local observability queries."""

    def __init__(self) -> None:
        self._records: list[OperationalLogRecord] = []
        self._lock = RLock()

    def record_span(self, span: SpanRecord) -> OperationalLogRecord:
        record = OperationalLogRecord(
            log_id=f"LOG-{uuid4().hex}",
            time=span.ended_at,
            level="ERROR" if span.exception_type else "INFO",
            message="span.completed",
            component=span.component,
            component_version=span.component_version,
            action=span.action,
            status=span.status.value,
            duration_ms=span.duration_ms,
            trace_id=span.trace_id,
            span_id=span.span_id,
            exception_type=span.exception_type,
        )
        with self._lock:
            self._records.append(record)
        return record

    def query(
        self,
        *,
        object_id: str,
        start_time: datetime,
        end_time: datetime,
        trace_id: str | None = None,
        status: str | None = None,
    ) -> list[OperationalLogRecord]:
        with self._lock:
            records = list(self._records)
        return [
            record
            for record in records
            if record.component == object_id
            and start_time <= record.time <= end_time
            and (trace_id is None or record.trace_id == trace_id)
            and (status is None or record.status == status)
        ]

    def list_all(self) -> list[OperationalLogRecord]:
        with self._lock:
            return list(self._records)

    def clear(self) -> None:
        with self._lock:
            self._records.clear()
