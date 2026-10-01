"""Local structured logging, tracing, and metric aggregation."""

from order_agent_ops.telemetry.logging import JsonFormatter, configure_json_logging
from order_agent_ops.telemetry.log_store import (
    OperationalLogRecord,
    OperationalLogStore,
)
from order_agent_ops.telemetry.metrics import MetricRegistry, MetricSummary
from order_agent_ops.telemetry.tracing import (
    SpanHandle,
    SpanRecord,
    SpanStatus,
    TraceRecorder,
)

__all__ = [
    "JsonFormatter",
    "MetricRegistry",
    "MetricSummary",
    "OperationalLogRecord",
    "OperationalLogStore",
    "SpanHandle",
    "SpanRecord",
    "SpanStatus",
    "TraceRecorder",
    "configure_json_logging",
]
