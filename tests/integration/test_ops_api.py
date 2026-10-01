from pathlib import Path

from fastapi.testclient import TestClient

from order_agent_ops.main import create_app
from order_agent_ops.models import MockModelProvider


def order_payload(key: str) -> dict[str, object]:
    return {
        "user_id": "USER-001",
        "items": [
            {
                "sku": "SKU-001",
                "quantity": 1,
                "unit_price": "399.00",
            }
        ],
        "total_amount": "399.00",
        "idempotency_key": key,
    }


def approval_payload(
    *,
    action: str,
    target: str,
    evidence_ids: list[str],
    target_version: str | None = None,
) -> dict[str, object]:
    return {
        "action": action,
        "target": target,
        "target_version": target_version,
        "risk_level": "high",
        "impact_scope": "Classroom recovery demonstration",
        "reason_evidence_ids": evidence_ids,
        "requested_by": "ops-guardian-agent",
    }


def test_complete_fault_recovery_flow_is_available_through_api(
    tmp_path: Path,
) -> None:
    application = create_app(
        database_path=tmp_path / "ops-api.sqlite3",
        provider=MockModelProvider(),
    )

    with TestClient(application) as client:
        health = client.get("/ops/health")
        assert health.status_code == 200
        assert health.json()["model_provider"] == "mock"

        fault = client.post("/ops/faults/inventory_v21_latency/activate")
        assert fault.status_code == 200
        assert fault.json()["scenario"]["active"] is True

        slow_order = client.post(
            "/orders",
            json=order_payload("ops-api-slow-order"),
        )
        assert slow_order.status_code == 200

        detected = client.post("/ops/detect")
        assert detected.status_code == 200
        latency_incidents = [
            item
            for item in detected.json()
            if item["rule"] == "inventory_p95_latency"
        ]
        assert len(latency_incidents) == 1
        incident_id = latency_incidents[0]["incident_id"]

        diagnosis = client.post(f"/ops/incidents/{incident_id}/diagnose")
        assert diagnosis.status_code == 200
        diagnosed = diagnosis.json()
        assert diagnosed["incident"]["status"] == "DIAGNOSED"
        result = diagnosed["incident"]["diagnosis"]
        assert result["facts"]
        assert result["root_cause_candidates"]
        assert result["missing_information"] == []
        evidence_ids = result["recommended_actions"][0]["evidence_ids"]

        detail = client.get(f"/ops/incidents/{incident_id}")
        assert detail.status_code == 200
        assert detail.json()["incident"]["diagnosis"] == result
        assert {
            item["source"]["type"] for item in detail.json()["evidence"]
        }.issuperset({"metric", "trace", "deployment"})

        forced_timeout = client.post(
            "/ops/faults/inventory_forced_timeout/activate"
        )
        assert forced_timeout.status_code == 200
        pending = client.post(
            "/orders",
            json=order_payload("ops-api-pending-order"),
        ).json()
        assert pending["status"] == "PENDING"

        rejected_action = client.post(
            "/ops/actions",
            json={
                "action": "rollback",
                "target": "inventory-agent",
                "target_version": "v2.0",
                "reason_evidence_ids": evidence_ids,
                "idempotency_key": "ops-api-unapproved-rollback",
                "incident_id": incident_id,
            },
        )
        assert rejected_action.status_code == 200
        assert rejected_action.json()["status"] == "rejected"
        assert rejected_action.json()["error_code"] == "HUMAN_APPROVAL_REQUIRED"

        rejected_approval = client.post(
            "/ops/approvals",
            json=approval_payload(
                action="rollback",
                target="inventory-agent",
                target_version="v2.0",
                evidence_ids=evidence_ids,
            ),
        ).json()
        rejected_decision = client.post(
            f"/ops/approvals/{rejected_approval['approval_id']}/decision",
            json={"approved": False, "decided_by": "classroom-operator"},
        )
        assert rejected_decision.status_code == 200
        assert rejected_decision.json()["approval"]["status"] == "rejected"
        assert rejected_decision.json()["approval_token"] is None

        approval = client.post(
            "/ops/approvals",
            json=approval_payload(
                action="rollback",
                target="inventory-agent",
                target_version="v2.0",
                evidence_ids=evidence_ids,
            ),
        )
        assert approval.status_code == 200
        approval_id = approval.json()["approval_id"]
        assert "approval_token_hash" not in approval.json()
        decision = client.post(
            f"/ops/approvals/{approval_id}/decision",
            json={"approved": True, "decided_by": "classroom-operator"},
        )
        assert decision.status_code == 200
        token = decision.json()["approval_token"]
        assert token.startswith("APPROVAL-TOKEN-")

        action = client.post(
            "/ops/actions",
            json={
                "action": "rollback",
                "target": "inventory-agent",
                "target_version": "v2.0",
                "approval_id": approval_id,
                "approval_token": token,
                "reason_evidence_ids": evidence_ids,
                "idempotency_key": "ops-api-approved-rollback",
                "incident_id": incident_id,
            },
        )
        assert action.status_code == 200
        assert action.json()["status"] == "succeeded"
        assert action.json()["before_state"]["version"] == "v2.1"
        assert action.json()["after_state"]["version"] == "v2.0"

        verification = client.post(f"/ops/incidents/{incident_id}/verify")
        assert verification.status_code == 200
        assert verification.json()["success"] is True
        assert verification.json()["incident"]["status"] == "RESOLVED"

        missing_replay_approval = client.post(
            f"/orders/{pending['order_id']}/replay",
            json={},
        )
        assert missing_replay_approval.status_code == 403
        assert missing_replay_approval.json()["error_code"] == (
            "HUMAN_APPROVAL_REQUIRED"
        )

        replay_approval = client.post(
            "/ops/approvals",
            json=approval_payload(
                action="replay",
                target=pending["order_id"],
                evidence_ids=evidence_ids,
            ),
        ).json()
        replay_decision = client.post(
            f"/ops/approvals/{replay_approval['approval_id']}/decision",
            json={"approved": True, "decided_by": "classroom-operator"},
        ).json()
        replayed = client.post(
            f"/orders/{pending['order_id']}/replay",
            json={
                "approval_id": replay_approval["approval_id"],
                "approval_token": replay_decision["approval_token"],
            },
        )
        assert replayed.status_code == 200
        assert replayed.json()["order"]["status"] == "COMPLETED"
        assert replayed.json()["order"]["order_id"] == pending["order_id"]
        assert replayed.json()["idempotent_replay"] is False

        repeated = client.post(
            f"/orders/{pending['order_id']}/replay",
            json={},
        )
        assert repeated.status_code == 200
        assert repeated.json()["idempotent_replay"] is True

        trace = client.get(
            f"/ops/traces/{replayed.json()['replay_trace_id']}"
        )
        assert trace.status_code == 200
        assert any(
            span["action"] == "order.replay" for span in trace.json()["spans"]
        )
        assert len(client.get("/ops/orders").json()) == 2
        assert client.get("/ops/actions").json()
        assert client.get("/ops/evidence", params={"incident_id": incident_id}).json()


def test_approval_rejection_structured_errors_and_dashboard_are_safe(
    tmp_path: Path,
) -> None:
    application = create_app(
        database_path=tmp_path / "ops-api-errors.sqlite3",
        provider=MockModelProvider(),
    )

    with TestClient(application) as client:
        before = client.get("/ops/health").json()["counts"]
        root_page = client.get("/")
        first_page = client.get("/ops/ui")
        second_page = client.get("/ops/ui")
        after = client.get("/ops/health").json()["counts"]

        assert root_page.status_code == 200
        assert first_page.status_code == second_page.status_code == 200
        assert "订单多智能体运维台" in first_page.text
        assert "/ops/approvals/" in first_page.text
        assert before == after

        missing_incident = client.get("/ops/incidents/INC-NOT-FOUND")
        missing_trace = client.get("/ops/traces/TRACE-NOT-FOUND")
        missing_approval = client.post(
            "/ops/approvals/APPROVAL-NOT-FOUND/decision",
            json={"approved": False, "decided_by": "operator"},
        )
        missing_fault = client.post("/ops/faults/not-a-scenario/activate")

    assert missing_incident.status_code == 404
    assert missing_incident.json()["error_code"] == "INCIDENT_NOT_FOUND"
    assert missing_trace.status_code == 404
    assert missing_trace.json()["error_code"] == "TRACE_NOT_FOUND"
    assert missing_approval.status_code == 404
    assert missing_approval.json()["error_code"] == "APPROVAL_NOT_FOUND"
    assert missing_fault.status_code == 404
    assert missing_fault.json()["error_code"] == "FAULT_SCENARIO_NOT_FOUND"
