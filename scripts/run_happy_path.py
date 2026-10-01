"""Run the natural-language shopping workflow and export its report and trace."""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

from order_agent_ops.main import create_app
from order_agent_ops.models import MockModelProvider

if __package__:
    from scripts.demo_support import (
        DEFAULT_ARTIFACTS_ROOT,
        ApiClient,
        DemoStageError,
        agent_execution_order,
        configure_utf8_stdout,
        relative_artifact_path,
        request_json,
        write_json_artifact,
    )
    from scripts.reset_demo import DEFAULT_DATABASE_PATH, reset_demo_environment
    from scripts.shopping_workflow_renderer import (
        render_shopping_workflow,
        render_workflow_error,
    )
else:  # Allow `python scripts/run_happy_path.py` from the project root.
    from demo_support import (
        DEFAULT_ARTIFACTS_ROOT,
        ApiClient,
        DemoStageError,
        agent_execution_order,
        configure_utf8_stdout,
        relative_artifact_path,
        request_json,
        write_json_artifact,
    )
    from reset_demo import DEFAULT_DATABASE_PATH, reset_demo_environment
    from shopping_workflow_renderer import (
        render_shopping_workflow,
        render_workflow_error,
    )


DEFAULT_MESSAGE = "我想买一个1000 元以内、适合晚上在宿舍写代码的机械键盘"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 180.0
WORKFLOW_ARTIFACT_PREFIX = "shopping_workflow"
_WORKFLOW_ARTIFACT_PATTERN = re.compile(
    rf"^{WORKFLOW_ARTIFACT_PREFIX}_(?:report|trace)_(\d+)\.json$"
)


def _positive_timeout(value: str) -> float:
    timeout = float(value)
    if timeout <= 0:
        raise argparse.ArgumentTypeError("request timeout must be greater than zero")
    return timeout


def next_workflow_artifact_number(artifacts_root: Path) -> int:
    """Return the next shared report/trace number without replacing prior runs."""

    existing_numbers: list[int] = []
    root = Path(artifacts_root)
    for directory_name in ("reports", "traces"):
        directory = root / directory_name
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            if not path.is_file():
                continue
            match = _WORKFLOW_ARTIFACT_PATTERN.fullmatch(path.name)
            if match is not None:
                existing_numbers.append(int(match.group(1)))
    return max(existing_numbers, default=0) + 1


def submit_happy_path(
    base_url: str,
    *,
    idempotency_key: str,
    client: httpx.Client | None = None,
) -> dict[str, object]:
    """Keep the stage-9 direct-order helper available for existing callers."""

    payload = {
        "user_id": "USER-001",
        "items": [{"sku": "SKU-001", "quantity": 1, "unit_price": "399.00"}],
        "total_amount": "399.00",
        "idempotency_key": idempotency_key,
    }
    owns_client = client is None
    active_client = client or httpx.Client(base_url=base_url, timeout=10.0)
    try:
        response = active_client.post("/orders", json=payload)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise RuntimeError("Order API returned a non-object response")
        return result
    finally:
        if owns_client:
            active_client.close()


def run_happy_path(
    client: ApiClient,
    *,
    message: str = DEFAULT_MESSAGE,
    idempotency_key: str = "demo-happy-confirmation",
    artifacts_root: Path = DEFAULT_ARTIFACTS_ROOT,
) -> dict[str, Any]:
    """Run natural-language intent, catalog confirmation, and the order workflow."""

    started = time.perf_counter()
    intent = request_json(
        client,
        "POST",
        "/shopping/intents",
        stage="create_purchase_intent",
        json={"user_id": "USER-001", "message": message},
    )
    if intent.get("status") != "READY_FOR_CONFIRMATION":
        raise DemoStageError(
            "create_purchase_intent",
            f"intent is not ready for confirmation: {intent.get('workflow_message')}",
        )
    candidates = intent.get("candidates") or []
    recommendation = intent.get("recommendation") or {}
    selected_sku = recommendation.get("recommended_sku")
    selected = next(
        (candidate for candidate in candidates if candidate.get("sku") == selected_sku),
        None,
    )
    if selected is None:
        raise DemoStageError(
            "select_catalog_candidate",
            "recommended SKU is missing from the server-provided candidate set",
        )

    confirmation = request_json(
        client,
        "POST",
        f"/shopping/intents/{intent['intent_id']}/confirm",
        stage="confirm_catalog_candidate",
        json={
            "sku": selected_sku,
            "quantity": 1,
            "idempotency_key": idempotency_key,
        },
    )
    order = confirmation.get("order") or {}
    if order.get("status") != "COMPLETED":
        raise DemoStageError(
            "submit_confirmed_order",
            f"expected COMPLETED order, got {order.get('status')}",
        )
    if order.get("items", [{}])[0].get("unit_price") != selected.get("unit_price"):
        raise DemoStageError(
            "verify_server_price",
            "confirmed order price differs from the saved catalog candidate",
        )

    trace_id = confirmation.get("trace_id")
    trace = request_json(
        client,
        "GET",
        f"/ops/traces/{trace_id}",
        stage="load_shopping_workflow_trace",
    )
    spans = trace.get("spans") or []
    elapsed_ms = max(0.0, (time.perf_counter() - started) * 1000)
    artifacts = Path(artifacts_root)
    artifact_number = next_workflow_artifact_number(artifacts)
    artifact_sequence = f"{artifact_number:03d}"
    report = {
        "scenario": "natural_language_shopping_workflow",
        "artifact_sequence": artifact_sequence,
        "model": {
            "provider": intent["model_provider"],
            "name": intent["model_name"],
            "analyze_prompt_version": intent["analyze_prompt_version"],
            "catalog_search_prompt_version": (
                intent.get("catalog_search_prompt_version")
                or intent["rank_prompt_version"]
            ),
            # Retained in exported reports for consumers of the original format.
            "rank_prompt_version": intent["rank_prompt_version"],
        },
        "natural_language_need": message,
        "structured_intent": intent["parsed_intent"],
        "candidates": candidates,
        "recommendation": recommendation,
        "confirmation": {
            "intent_id": intent["intent_id"],
            "sku": selected_sku,
            "quantity": 1,
            "server_unit_price": selected["unit_price"],
        },
        "order": {
            "order_id": order["order_id"],
            "status": order["status"],
            "total_amount": order["total_amount"],
        },
        "trace_id": trace_id,
        "agent_execution_order": agent_execution_order(spans),
        "execution_trace": [
            {
                "component": span.get("component"),
                "action": span.get("action"),
                "duration_ms": span.get("duration_ms"),
                "status": span.get("status"),
            }
            for span in spans
        ],
        "total_duration_ms": round(elapsed_ms, 3),
    }
    trace_path = write_json_artifact(
        artifacts
        / "traces"
        / f"{WORKFLOW_ARTIFACT_PREFIX}_trace_{artifact_sequence}.json",
        trace,
    )
    report_path = write_json_artifact(
        artifacts
        / "reports"
        / f"{WORKFLOW_ARTIFACT_PREFIX}_report_{artifact_sequence}.json",
        report,
    )
    return {
        **report,
        "artifacts": {
            "trace": relative_artifact_path(trace_path),
            "report": relative_artifact_path(report_path),
        },
    }


def run_standalone_happy_path(
    *,
    database_path: Path = DEFAULT_DATABASE_PATH,
    artifacts_root: Path = DEFAULT_ARTIFACTS_ROOT,
    message: str = DEFAULT_MESSAGE,
    idempotency_key: str = "demo-happy-confirmation",
    reset: bool = True,
) -> dict[str, Any]:
    """Build an in-process Mock application so no separately running server is needed."""

    if reset:
        reset_demo_environment(database_path, artifacts_root)
    application = create_app(
        database_path=database_path,
        provider=MockModelProvider(),
    )
    with TestClient(application) as client:
        return run_happy_path(
            client,
            message=message,
            idempotency_key=idempotency_key,
            artifacts_root=artifacts_root,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=None,
        help="Use an already-running API instead of the default standalone Mock app.",
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--artifacts", type=Path, default=DEFAULT_ARTIFACTS_ROOT)
    parser.add_argument("--message", default=DEFAULT_MESSAGE)
    parser.add_argument("--idempotency-key", default="demo-happy-confirmation")
    parser.add_argument(
        "--output-format",
        choices=("pretty", "json"),
        default="pretty",
        help="Terminal output style (default: pretty; use json for machine input).",
    )
    parser.add_argument(
        "--request-timeout",
        type=_positive_timeout,
        default=DEFAULT_REQUEST_TIMEOUT_SECONDS,
        help="HTTP wait time in seconds when --base-url is used (default: 180).",
    )
    parser.add_argument(
        "--no-reset",
        action="store_true",
        help="Keep the selected standalone database instead of resetting it first.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_utf8_stdout()
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.base_url:
            with httpx.Client(
                base_url=arguments.base_url,
                timeout=arguments.request_timeout,
            ) as client:
                result = run_happy_path(
                    client,
                    message=arguments.message,
                    idempotency_key=arguments.idempotency_key,
                    artifacts_root=arguments.artifacts,
                )
        else:
            result = run_standalone_happy_path(
                database_path=arguments.database,
                artifacts_root=arguments.artifacts,
                message=arguments.message,
                idempotency_key=arguments.idempotency_key,
                reset=not arguments.no_reset,
            )
    except Exception as error:
        stage = error.stage if isinstance(error, DemoStageError) else "unexpected_error"
        payload = {"status": "failed", "stage": stage, "error": str(error)}
        if arguments.output_format == "json":
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(render_workflow_error(payload))
        return 1
    if arguments.output_format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render_shopping_workflow(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
