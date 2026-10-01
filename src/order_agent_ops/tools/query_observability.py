"""Read-only metrics, logs, and trace evidence query tool."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol
from uuid import uuid4

from pydantic import AwareDatetime, Field, model_validator

from order_agent_ops.domain.base import DomainModel, NonEmptyString
from order_agent_ops.domain.evidence import EvidenceTimeWindow
from order_agent_ops.ops.evidence_store import EvidenceDraft, EvidenceStore
from order_agent_ops.telemetry.log_store import OperationalLogStore
from order_agent_ops.telemetry.tracing import SpanRecord, SpanStatus, TraceRecorder
from order_agent_ops.tools.schemas import EvidenceQueryResult, QueryIssue, QueryStatus


class ObservabilityDataType(StrEnum):
    METRICS = "metrics"
    LOGS = "logs"
    TRACES = "traces"


class QueryObservabilityRequest(DomainModel):
    object_id: NonEmptyString
    start_time: AwareDatetime
    end_time: AwareDatetime
    data_types: list[ObservabilityDataType] = Field(min_length=1)
    filters: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_time_and_types(self) -> QueryObservabilityRequest:
        if self.end_time < self.start_time:
            raise ValueError("end_time must not precede start_time")
        if len(set(self.data_types)) != len(self.data_types):
            raise ValueError("data_types must not contain duplicates")
        return self


class ObservabilitySource(Protocol):
    def query(
        self,
        data_type: ObservabilityDataType,
        request: QueryObservabilityRequest,
        *,
        query_ref: str,
    ) -> Sequence[EvidenceDraft]: ...


class LocalObservabilitySource:
    """Query actual local span and structured-log records."""

    def __init__(
        self,
        trace_recorder: TraceRecorder,
        log_store: OperationalLogStore,
    ) -> None:
        self._traces = trace_recorder
        self._logs = log_store

    def query(
        self,
        data_type: ObservabilityDataType,
        request: QueryObservabilityRequest,
        *,
        query_ref: str,
    ) -> Sequence[EvidenceDraft]:
        if data_type is ObservabilityDataType.METRICS:
            return self._query_metrics(request, query_ref=query_ref)
        if data_type is ObservabilityDataType.LOGS:
            return self._query_logs(request, query_ref=query_ref)
        return self._query_traces(request, query_ref=query_ref)

    def _matching_spans(
        self, request: QueryObservabilityRequest
    ) -> list[SpanRecord]:
        trace_id = request.filters.get("trace_id")
        status = request.filters.get("status")
        return [
            span
            for span in self._traces.list_spans()
            if span.component == request.object_id
            and span.ended_at >= request.start_time
            and span.started_at <= request.end_time
            and (trace_id is None or span.trace_id == trace_id)
            and (status is None or span.status.value == status)
        ]

    def _query_metrics(
        self, request: QueryObservabilityRequest, *, query_ref: str
    ) -> list[EvidenceDraft]:
        grouped: dict[str | None, list[SpanRecord]] = defaultdict(list)
        for span in self._matching_spans(request):
            grouped[span.component_version].append(span)

        evidence: list[EvidenceDraft] = []
        for version, spans in grouped.items():
            durations = sorted(span.duration_ms for span in spans)
            rank = max(1, math.ceil(0.95 * len(durations)))
            p95 = durations[rank - 1]
            error_count = sum(span.status is SpanStatus.ERROR for span in spans)
            evidence.append(
                EvidenceDraft(
                    time=EvidenceTimeWindow(
                        start=request.start_time,
                        end=request.end_time,
                    ),
                    source={
                        "type": "metric",
                        "query_ref": f"{query_ref}:metrics",
                    },
                    object={"id": request.object_id, "version": version},
                    fact={
                        "observation": (
                            f"{request.object_id} P95 latency was {p95:.1f}ms "
                            f"across {len(spans)} spans"
                        ),
                        "p95_duration_ms": p95,
                        "sample_count": len(spans),
                        "error_count": error_count,
                        "actions": sorted({span.action for span in spans}),
                    },
                )
            )
        return evidence

    def _query_logs(
        self, request: QueryObservabilityRequest, *, query_ref: str
    ) -> list[EvidenceDraft]:
        trace_id = request.filters.get("trace_id")
        status = request.filters.get("status")
        records = self._logs.query(
            object_id=request.object_id,
            start_time=request.start_time,
            end_time=request.end_time,
            trace_id=trace_id,
            status=status,
        )
        return [
            EvidenceDraft(
                time=record.time,
                source={
                    "type": "log",
                    "query_ref": f"{query_ref}:logs:{record.log_id}",
                },
                object={
                    "id": record.component,
                    "version": record.component_version,
                },
                fact={
                    "observation": (
                        f"{record.message}: {record.action} finished "
                        f"with status {record.status} in {record.duration_ms:.1f}ms"
                    ),
                    "level": record.level,
                    "action": record.action,
                    "status": record.status,
                    "duration_ms": record.duration_ms,
                    "trace_id": record.trace_id,
                    "span_id": record.span_id,
                    "exception_type": record.exception_type,
                },
            )
            for record in records
        ]

    def _query_traces(
        self, request: QueryObservabilityRequest, *, query_ref: str
    ) -> list[EvidenceDraft]:
        return [
            EvidenceDraft(
                time=EvidenceTimeWindow(start=span.started_at, end=span.ended_at),
                source={
                    "type": "trace",
                    "query_ref": f"{query_ref}:traces:{span.span_id}",
                },
                object={"id": span.component, "version": span.component_version},
                fact={
                    "observation": (
                        f"Trace span {span.action} finished with status "
                        f"{span.status.value} in {span.duration_ms:.1f}ms"
                    ),
                    "trace_id": span.trace_id,
                    "span_id": span.span_id,
                    "parent_span_id": span.parent_span_id,
                    "action": span.action,
                    "status": span.status.value,
                    "duration_ms": span.duration_ms,
                    "exception_type": span.exception_type,
                },
            )
            for span in self._matching_spans(request)
        ]


class QueryObservabilityTool:
    """Convert read-only observability results into persisted evidence."""

    name = "query_observability"
    version = "v1"

    def __init__(
        self,
        source: ObservabilitySource,
        evidence_store: EvidenceStore,
        *,
        max_time_range: timedelta = timedelta(hours=24),
    ) -> None:
        self._source = source
        self._evidence_store = evidence_store
        self._max_time_range = max_time_range

    def execute(
        self,
        request: QueryObservabilityRequest,
        *,
        incident_id: str | None = None,
    ) -> EvidenceQueryResult:
        query_ref = f"OBS-{uuid4().hex}"
        if request.end_time - request.start_time > self._max_time_range:
            return EvidenceQueryResult(
                status=QueryStatus.INVALID_REQUEST,
                query_ref=query_ref,
                evidence=[],
                issues=[
                    QueryIssue(
                        source=self.name,
                        error_code="TIME_RANGE_TOO_LARGE",
                        message=(
                            f"Requested range exceeds {self._max_time_range}"
                        ),
                    )
                ],
            )

        drafts: list[EvidenceDraft] = []
        issues: list[QueryIssue] = []
        successful_sources = 0
        for data_type in request.data_types:
            try:
                results = self._source.query(
                    data_type,
                    request,
                    query_ref=query_ref,
                )
            except TimeoutError:
                issues.append(
                    QueryIssue(
                        source=data_type.value,
                        error_code="QUERY_TIMEOUT",
                        message=f"{data_type.value} query timed out",
                    )
                )
            except Exception as error:
                issues.append(
                    QueryIssue(
                        source=data_type.value,
                        error_code="DATA_SOURCE_UNAVAILABLE",
                        message=(
                            f"{data_type.value} source failed with "
                            f"{type(error).__name__}"
                        ),
                    )
                )
            else:
                successful_sources += 1
                drafts.extend(results)

        evidence = self._evidence_store.save_many(
            drafts,
            incident_id=incident_id,
        )
        if issues and successful_sources:
            status = QueryStatus.PARTIAL
        elif issues:
            status = (
                QueryStatus.TIMEOUT
                if all(issue.error_code == "QUERY_TIMEOUT" for issue in issues)
                else QueryStatus.UNAVAILABLE
            )
        elif evidence:
            status = QueryStatus.SUCCESS
        else:
            status = QueryStatus.NO_DATA
            issues.append(
                QueryIssue(
                    source=self.name,
                    error_code="NO_DATA",
                    message="No matching observability records were found",
                )
            )
        return EvidenceQueryResult(
            status=status,
            query_ref=query_ref,
            evidence=evidence,
            issues=issues,
        )
