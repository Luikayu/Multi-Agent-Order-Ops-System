import io
import json
import logging

from order_agent_ops.telemetry.logging import configure_json_logging


def test_json_log_contains_structured_fields_and_redacts_sensitive_context() -> None:
    output = io.StringIO()
    logger = configure_json_logging(logging.Logger("telemetry-test"), stream=output)

    logger.info(
        "span.completed",
        extra={
            "trace_id": "TRACE-001",
            "span_id": "SPAN-001",
            "parent_span_id": None,
            "component": "inventory-agent",
            "action": "inventory.check",
            "status": "ok",
            "duration_ms": 12.5,
            "context": {
                "api_key": "super-secret-key",
                "approval_token": "approval-secret",
                "payload": {"user_id": "USER-001", "card": "full-input"},
                "retry_count": 0,
            },
        },
    )

    rendered = output.getvalue().strip()
    payload = json.loads(rendered)

    assert payload["level"] == "INFO"
    assert payload["message"] == "span.completed"
    assert payload["trace_id"] == "TRACE-001"
    assert payload["component"] == "inventory-agent"
    assert payload["duration_ms"] == 12.5
    assert payload["context"] == {
        "api_key": "[REDACTED]",
        "approval_token": "[REDACTED]",
        "payload": "[OMITTED]",
        "retry_count": 0,
    }
    assert "super-secret-key" not in rendered
    assert "approval-secret" not in rendered
    assert "full-input" not in rendered


def test_exception_log_keeps_type_without_traceback_or_exception_message() -> None:
    output = io.StringIO()
    logger = configure_json_logging(logging.Logger("exception-test"), stream=output)

    try:
        raise RuntimeError("sensitive exception detail")
    except RuntimeError:
        logger.exception("operation failed")

    rendered = output.getvalue().strip()
    payload = json.loads(rendered)

    assert payload["message"] == "operation failed"
    assert payload["exception_type"] == "RuntimeError"
    assert "sensitive exception detail" not in rendered
    assert "Traceback" not in rendered
