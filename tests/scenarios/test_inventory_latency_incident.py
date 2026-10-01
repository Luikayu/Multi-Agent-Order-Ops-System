import json
from pathlib import Path

from scripts.run_inventory_latency_incident import run_inventory_latency_incident


def test_inventory_latency_incident_closes_the_recovery_loop_repeatably(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "incident.sqlite3"
    artifacts_root = tmp_path / "artifacts"

    first = run_inventory_latency_incident(
        database_path=database_path,
        artifacts_root=artifacts_root,
    )
    second = run_inventory_latency_incident(
        database_path=database_path,
        artifacts_root=artifacts_root,
    )

    for result in (first, second):
        assert result["fault"]["component_version"] == "v2.1"
        assert result["incident"]["incident"]["status"] == "RESOLVED"
        assert {
            item["source"]["type"] for item in result["evidence_chain"]
        }.issuperset({"metric", "trace", "deployment"})
        assert result["unapproved_action"]["status"] == "rejected"
        assert result["unapproved_action"]["error_code"] == (
            "HUMAN_APPROVAL_REQUIRED"
        )
        assert result["approved_rollback"]["status"] == "succeeded"
        assert result["approved_rollback"]["after_state"]["version"] == "v2.0"
        assert result["verification"]["success"] is True
        assert result["pending_order_before_replay"]["status"] == "PENDING"
        assert result["replay"]["order"]["status"] == "COMPLETED"
        assert result["audit_events"]

    report_path = (
        artifacts_root / "reports" / "inventory_latency_incident_report.json"
    )
    traces_path = (
        artifacts_root / "traces" / "inventory_latency_incident_traces.json"
    )
    assert report_path.is_file()
    assert traces_path.is_file()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    traces = json.loads(traces_path.read_text(encoding="utf-8"))
    assert report["incident"]["incident"]["status"] == "RESOLVED"
    assert len(traces["traces"]) >= 5
    exported = report_path.read_text(encoding="utf-8")
    assert "APPROVAL-TOKEN-" not in exported
    assert "approval_token_hash" not in exported
    assert "api_key" not in exported.lower()
