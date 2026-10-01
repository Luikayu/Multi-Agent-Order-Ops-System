"""Run the complete inventory-v2.1 latency incident and recovery experiment."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from order_agent_ops.main import create_app
from order_agent_ops.models import MockModelProvider

if __package__:
    from scripts.demo_support import (
        DEFAULT_ARTIFACTS_ROOT,
        DemoStageError,
        configure_utf8_stdout,
        relative_artifact_path,
        request_json,
        write_json_artifact,
    )
    from scripts.reset_demo import DEFAULT_DATABASE_PATH, reset_demo_environment
else:  # Allow direct execution from the project root.
    from demo_support import (
        DEFAULT_ARTIFACTS_ROOT,
        DemoStageError,
        configure_utf8_stdout,
        relative_artifact_path,
        request_json,
        write_json_artifact,
    )
    from reset_demo import DEFAULT_DATABASE_PATH, reset_demo_environment


def _order_payload(key: str) -> dict[str, Any]:
    return {
        "user_id": "USER-001",
        "items": [{"sku": "SKU-001", "quantity": 1, "unit_price": "399.00"}],
        "total_amount": "399.00",
        "idempotency_key": key,
    }


def _approval_payload(
    *,
    action: str,
    target: str,
    evidence_ids: list[str],
    target_version: str | None = None,
) -> dict[str, Any]:
    return {
        "action": action,
        "target": target,
        "target_version": target_version,
        "risk_level": "high",
        "impact_scope": "Repeatable stage-18 classroom experiment",
        "reason_evidence_ids": evidence_ids,
        "requested_by": "ops-guardian-agent",
    }


def _require(condition: bool, stage: str, message: str) -> None:
    if not condition:
        raise DemoStageError(stage, message)


def _audit_export(application, entities: list[tuple[str, str]]) -> list[dict[str, Any]]:
    exported: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entity_type, entity_id in entities:
        for event in application.state.audit_repository.list_for_entity(
            entity_type,
            entity_id,
        ):
            if event.event_id in seen:
                continue
            seen.add(event.event_id)
            exported.append(event.model_dump(mode="json"))
    return exported


def run_inventory_latency_incident(
    *,
    database_path: Path = DEFAULT_DATABASE_PATH,
    artifacts_root: Path = DEFAULT_ARTIFACTS_ROOT,
    reset: bool = True,
) -> dict[str, Any]:
    """Exercise detect, diagnose, approval, rollback, verify, and replay via API."""

    if reset:
        reset_demo_environment(database_path, artifacts_root)
    application = create_app(
        database_path=database_path,
        provider=MockModelProvider(),
    )
    started = time.perf_counter()

    with TestClient(application) as client:
        latency_fault = request_json(
            client,
            "POST",
            "/ops/faults/inventory_v21_latency/activate",
            stage="activate_inventory_v21_latency",
        )
        _require(
            latency_fault["scenario"]["active"] is True
            and latency_fault["scenario"]["component_version"] == "v2.1",
            "activate_inventory_v21_latency",
            "inventory-agent v2.1 latency scenario did not become active",
        )

        slow_order = request_json(
            client,
            "POST",
            "/orders",
            stage="create_slow_test_order",
            json=_order_payload("demo-incident-slow-order"),
        )

        incidents = request_json(
            client,
            "POST",
            "/ops/detect",
            stage="detect_latency_incident",
        )
        incident = next(
            (item for item in incidents if item.get("rule") == "inventory_p95_latency"),
            None,
        )
        _require(
            incident is not None,
            "detect_latency_incident",
            "detector did not create an inventory P95 latency incident",
        )
        incident_id = incident["incident_id"]

        diagnosis = request_json(
            client,
            "POST",
            f"/ops/incidents/{incident_id}/diagnose",
            stage="diagnose_latency_incident",
        )
        diagnosis_result = diagnosis["incident"].get("diagnosis") or {}
        recommended_actions = diagnosis_result.get("recommended_actions") or []
        _require(
            bool(diagnosis_result.get("root_cause_candidates"))
            and bool(recommended_actions),
            "diagnose_latency_incident",
            "diagnosis did not contain a root cause and recommended action",
        )
        evidence_ids = recommended_actions[0]["evidence_ids"]
        evidence = diagnosis.get("evidence") or []
        evidence_types = {item["source"]["type"] for item in evidence}
        _require(
            {"metric", "trace", "deployment"}.issubset(evidence_types),
            "validate_evidence_chain",
            "diagnosis is missing metric, trace, or deployment evidence",
        )

        timeout_fault = request_json(
            client,
            "POST",
            "/ops/faults/inventory_forced_timeout/activate",
            stage="activate_pending_order_fault",
        )
        _require(
            timeout_fault["scenario"]["active"] is True,
            "activate_pending_order_fault",
            "forced-timeout scenario did not become active",
        )
        pending_order = request_json(
            client,
            "POST",
            "/orders",
            stage="create_pending_order",
            json=_order_payload("demo-incident-pending-order"),
        )
        _require(
            pending_order.get("status") == "PENDING",
            "create_pending_order",
            f"expected PENDING order, got {pending_order.get('status')}",
        )

        unapproved_action = request_json(
            client,
            "POST",
            "/ops/actions",
            stage="demonstrate_unapproved_rejection",
            json={
                "action": "rollback",
                "target": "inventory-agent",
                "target_version": "v2.0",
                "reason_evidence_ids": evidence_ids,
                "idempotency_key": "demo-unapproved-rollback",
                "incident_id": incident_id,
            },
        )
        _require(
            unapproved_action.get("status") == "rejected"
            and unapproved_action.get("error_code") == "HUMAN_APPROVAL_REQUIRED",
            "demonstrate_unapproved_rejection",
            "rollback without approval was not rejected by the permission gate",
        )

        approval = request_json(
            client,
            "POST",
            "/ops/approvals",
            stage="create_rollback_approval",
            json=_approval_payload(
                action="rollback",
                target="inventory-agent",
                target_version="v2.0",
                evidence_ids=evidence_ids,
            ),
        )
        approval_decision = request_json(
            client,
            "POST",
            f"/ops/approvals/{approval['approval_id']}/decision",
            stage="approve_rollback",
            json={"approved": True, "decided_by": "classroom-operator"},
        )
        approval_token = approval_decision.get("approval_token")
        _require(
            isinstance(approval_token, str) and bool(approval_token),
            "approve_rollback",
            "approved rollback did not return its one-time token",
        )

        approved_action = request_json(
            client,
            "POST",
            "/ops/actions",
            stage="execute_approved_rollback",
            json={
                "action": "rollback",
                "target": "inventory-agent",
                "target_version": "v2.0",
                "approval_id": approval["approval_id"],
                "approval_token": approval_token,
                "reason_evidence_ids": evidence_ids,
                "idempotency_key": "demo-approved-rollback",
                "incident_id": incident_id,
            },
        )
        approval_token = None
        _require(
            approved_action.get("status") == "succeeded"
            and approved_action.get("after_state", {}).get("version") == "v2.0",
            "execute_approved_rollback",
            "approved simulated rollback did not restore inventory-agent v2.0",
        )

        verification = request_json(
            client,
            "POST",
            f"/ops/incidents/{incident_id}/verify",
            stage="verify_recovery",
        )
        _require(
            verification.get("success") is True
            and verification.get("incident", {}).get("status") == "RESOLVED",
            "verify_recovery",
            "post-remediation verification did not resolve the incident",
        )

        replay_approval = request_json(
            client,
            "POST",
            "/ops/approvals",
            stage="create_replay_approval",
            json=_approval_payload(
                action="replay",
                target=pending_order["order_id"],
                evidence_ids=evidence_ids,
            ),
        )
        replay_decision = request_json(
            client,
            "POST",
            f"/ops/approvals/{replay_approval['approval_id']}/decision",
            stage="approve_pending_order_replay",
            json={"approved": True, "decided_by": "classroom-operator"},
        )
        replay_token = replay_decision.get("approval_token")
        _require(
            isinstance(replay_token, str) and bool(replay_token),
            "approve_pending_order_replay",
            "approved replay did not return its one-time token",
        )
        replay = request_json(
            client,
            "POST",
            f"/orders/{pending_order['order_id']}/replay",
            stage="replay_pending_order",
            json={
                "approval_id": replay_approval["approval_id"],
                "approval_token": replay_token,
            },
        )
        replay_token = None
        _require(
            replay.get("order", {}).get("status") == "COMPLETED",
            "replay_pending_order",
            "pending order did not complete after safe replay",
        )

        final_incident = request_json(
            client,
            "GET",
            f"/ops/incidents/{incident_id}",
            stage="export_final_incident",
        )
        approvals = request_json(
            client,
            "GET",
            "/ops/approvals",
            stage="export_approvals",
        )
        actions = request_json(
            client,
            "GET",
            "/ops/actions",
            stage="export_actions",
        )

        trace_ids = [
            slow_order["trace_id"],
            diagnosis["trace_id"],
            pending_order["trace_id"],
            verification["trace_id"],
            replay["replay_trace_id"],
        ]
        traces = [
            request_json(
                client,
                "GET",
                f"/ops/traces/{trace_id}",
                stage=f"export_trace_{trace_id}",
            )
            for trace_id in dict.fromkeys(trace_ids)
        ]

    audit_entities = [
        ("incident", incident_id),
        ("order", slow_order["order_id"]),
        ("order", pending_order["order_id"]),
        ("approval", approval["approval_id"]),
        ("approval", replay_approval["approval_id"]),
        ("remediation_action", unapproved_action["action_id"]),
        ("remediation_action", approved_action["action_id"]),
    ]
    audit_events = _audit_export(application, audit_entities)
    elapsed_ms = max(0.0, (time.perf_counter() - started) * 1000)
    report = {
        "scenario": "inventory_v21_latency_incident",
        "fault": latency_fault["scenario"],
        "slow_order": slow_order,
        "incident": final_incident,
        "diagnosis_trace_id": diagnosis["trace_id"],
        "evidence_chain": evidence,
        "unapproved_action": unapproved_action,
        "approved_rollback": approved_action,
        "approvals": approvals,
        "verification": verification,
        "pending_order_before_replay": pending_order,
        "replay": replay,
        "audit_events": audit_events,
        "total_duration_ms": round(elapsed_ms, 3),
    }
    artifacts = Path(artifacts_root)
    traces_path = write_json_artifact(
        artifacts / "traces" / "inventory_latency_incident_traces.json",
        {"scenario": report["scenario"], "traces": traces},
    )
    report_path = write_json_artifact(
        artifacts / "reports" / "inventory_latency_incident_report.json",
        report,
    )
    return {
        **report,
        "artifacts": {
            "traces": relative_artifact_path(traces_path),
            "report": relative_artifact_path(report_path),
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--artifacts", type=Path, default=DEFAULT_ARTIFACTS_ROOT)
    parser.add_argument(
        "--no-reset",
        action="store_true",
        help="Keep the selected database instead of resetting it first.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_utf8_stdout()
    arguments = build_parser().parse_args(argv)
    try:
        result = run_inventory_latency_incident(
            database_path=arguments.database,
            artifacts_root=arguments.artifacts,
            reset=not arguments.no_reset,
        )
    except Exception as error:
        stage = error.stage if isinstance(error, DemoStageError) else "unexpected_error"
        print(
            json.dumps(
                {"status": "failed", "stage": stage, "error": str(error)},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
