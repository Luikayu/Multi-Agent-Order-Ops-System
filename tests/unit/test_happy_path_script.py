import json
from pathlib import Path

import httpx

from scripts.run_happy_path import (
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    build_parser,
    next_workflow_artifact_number,
    submit_happy_path,
)


def test_happy_path_script_posts_expected_request_and_returns_api_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/orders"
        payload = json.loads(request.content)
        assert payload["user_id"] == "USER-001"
        assert payload["idempotency_key"] == "script-test-key"
        return httpx.Response(
            200,
            json={
                "request_id": "REQ-SCRIPT",
                "order_id": "ORD-SCRIPT",
                "trace_id": "TRACE-SCRIPT",
                "status": "COMPLETED",
                "idempotent_replay": False,
            },
        )

    with httpx.Client(
        base_url="http://testserver",
        transport=httpx.MockTransport(handler),
    ) as client:
        result = submit_happy_path(
            "http://testserver",
            idempotency_key="script-test-key",
            client=client,
        )

    assert result["status"] == "COMPLETED"
    assert result["trace_id"] == "TRACE-SCRIPT"


def test_happy_path_script_has_configurable_long_request_timeout() -> None:
    defaults = build_parser().parse_args([])
    overridden = build_parser().parse_args(["--request-timeout", "240"])
    json_output = build_parser().parse_args(["--output-format", "json"])

    assert defaults.request_timeout == DEFAULT_REQUEST_TIMEOUT_SECONDS == 180.0
    assert defaults.output_format == "pretty"
    assert overridden.request_timeout == 240.0
    assert json_output.output_format == "json"


def test_workflow_artifact_number_uses_maximum_existing_report_or_trace(
    tmp_path: Path,
) -> None:
    reports = tmp_path / "reports"
    traces = tmp_path / "traces"
    reports.mkdir()
    traces.mkdir()
    (reports / "shopping_workflow_report_001.json").write_text("{}")
    (traces / "shopping_workflow_trace_004.json").write_text("{}")
    (reports / "unrelated_report_999.json").write_text("{}")
    (reports / "happy_path_report.json").write_text("{}")

    assert next_workflow_artifact_number(tmp_path) == 5
