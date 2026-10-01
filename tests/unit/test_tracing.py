import io
import json
import logging

import pytest

from order_agent_ops.telemetry.logging import configure_json_logging
from order_agent_ops.telemetry.metrics import MetricRegistry
from order_agent_ops.telemetry.tracing import SpanStatus, TraceRecorder


def test_nested_spans_have_correct_parent_child_relationship() -> None:
    recorder = TraceRecorder()

    with recorder.span("TRACE-001", "order-api", "order.request") as root:
        with recorder.span("TRACE-001", "inventory-agent", "inventory.check") as child:
            assert child.parent_span_id == root.span_id

    spans = recorder.get_trace("TRACE-001")

    assert len(spans) == 2
    assert spans[0].span_id == root.span_id
    assert spans[0].parent_span_id is None
    assert spans[1].span_id == child.span_id
    assert spans[1].parent_span_id == root.span_id
    assert all(span.status is SpanStatus.OK for span in spans)
    assert all(span.ended_at >= span.started_at for span in spans)
    assert all(span.duration_ms >= 0 for span in spans)


def test_exception_marks_span_error_and_is_rethrown() -> None:
    recorder = TraceRecorder()

    with pytest.raises(RuntimeError, match="inventory unavailable"):
        with recorder.span("TRACE-ERROR", "inventory-agent", "inventory.query"):
            raise RuntimeError("inventory unavailable")

    span = recorder.get_trace("TRACE-ERROR")[0]
    assert span.status is SpanStatus.ERROR
    assert span.exception_type == "RuntimeError"


def test_trace_query_returns_only_requested_execution_path() -> None:
    recorder = TraceRecorder()

    with recorder.span("TRACE-001", "order-api", "order.request"):
        with recorder.span("TRACE-002", "risk-agent", "risk.evaluate") as other_trace:
            assert other_trace.parent_span_id is None

    assert len(recorder.get_trace("TRACE-001")) == 1
    assert len(recorder.get_trace("TRACE-002")) == 1
    assert recorder.get_trace("TRACE-MISSING") == []


def test_completed_spans_update_metrics_and_emit_json_log() -> None:
    metrics = MetricRegistry()
    output = io.StringIO()
    logger = configure_json_logging(logging.Logger("trace-test"), stream=output)
    recorder = TraceRecorder(metrics=metrics, logger=logger)

    with recorder.span("TRACE-001", "inventory-agent", "inventory.check"):
        pass

    summary = metrics.summary("inventory-agent.inventory.check")
    log_record = json.loads(output.getvalue())

    assert summary.count == 1
    assert summary.error_count == 0
    assert summary.p95_duration_ms is not None
    assert log_record["trace_id"] == "TRACE-001"
    assert log_record["status"] == "ok"
    assert log_record["component"] == "inventory-agent"


def test_generated_trace_id_uses_project_prefix() -> None:
    assert TraceRecorder.new_trace_id().startswith("TRACE-")


@pytest.mark.parametrize(("component", "action"), [("", "query"), ("agent", " ")])
def test_empty_span_identity_is_rejected(component: str, action: str) -> None:
    recorder = TraceRecorder()

    with pytest.raises(ValueError):
        with recorder.span("TRACE-001", component, action):
            pass

    assert recorder.list_spans() == []
