"""Safe JSON logging without an external logging platform."""

import json
import logging
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, TextIO


REDACTED = "[REDACTED]"
OMITTED = "[OMITTED]"
MAX_LOG_STRING_LENGTH = 1024
SENSITIVE_KEY_FRAGMENTS = (
    "api_key",
    "apikey",
    "authorization",
    "password",
    "secret",
    "token",
)
OMITTED_CONTENT_KEYS = {
    "input",
    "messages",
    "payload",
    "prompt",
    "request_body",
    "response_body",
}
STRUCTURED_FIELDS = (
    "trace_id",
    "span_id",
    "parent_span_id",
    "component",
    "action",
    "status",
    "duration_ms",
)


def _truncate(value: str) -> str:
    if len(value) <= MAX_LOG_STRING_LENGTH:
        return value
    return f"{value[:MAX_LOG_STRING_LENGTH]}...[TRUNCATED]"


def sanitize_for_logging(value: Any, *, field_name: str | None = None) -> Any:
    """Recursively redact secrets and omit complete model/request content."""

    normalized_name = (field_name or "").lower()
    if any(fragment in normalized_name for fragment in SENSITIVE_KEY_FRAGMENTS):
        return REDACTED
    if normalized_name in OMITTED_CONTENT_KEYS:
        return OMITTED
    if isinstance(value, Mapping):
        return {
            str(key): sanitize_for_logging(item, field_name=str(key))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_for_logging(item) for item in value]
    if isinstance(value, str):
        return _truncate(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _truncate(str(value))


class JsonFormatter(logging.Formatter):
    """Emit one compact JSON object per log event."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": _truncate(record.getMessage()),
        }

        for field in STRUCTURED_FIELDS:
            if hasattr(record, field):
                payload[field] = sanitize_for_logging(getattr(record, field))

        context = getattr(record, "context", None)
        if isinstance(context, Mapping):
            payload["context"] = sanitize_for_logging(context)

        explicit_exception_type = getattr(record, "exception_type", None)
        if explicit_exception_type:
            payload["exception_type"] = str(explicit_exception_type)
        elif record.exc_info and record.exc_info[0]:
            payload["exception_type"] = record.exc_info[0].__name__

        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_json_logging(
    logger: logging.Logger,
    *,
    level: int = logging.INFO,
    stream: TextIO | None = None,
) -> logging.Logger:
    """Configure a supplied logger with exactly one local JSON handler."""

    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return logger
