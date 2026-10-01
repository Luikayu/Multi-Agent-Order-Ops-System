from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from order_agent_ops.domain.enums import IncidentStatus
from order_agent_ops.domain.incidents import IncidentRecord
from order_agent_ops.faults import FaultController
from order_agent_ops.main import create_app
from order_agent_ops.models import MockModelProvider
from order_agent_ops.models.provider import ModelTaskType
from order_agent_ops.ops.evidence_validator import UnknownEvidenceReferenceError


def order_payload(key: str) -> dict[str, object]:
    return {
        "user_id": "USER-001",
        "items": [
            {"sku": "SKU-001", "quantity": 1, "unit_price": "399.00"}
        ],
        "total_amount": "399.00",
        "idempotency_key": key,
    }


def test_inventory_latency_incident_gets_evidence_backed_diagnosis_and_trace(
    tmp_path: Path,
) -> None:
    controller = FaultController.from_yaml()
    application = create_app(
        database_path=tmp_path / "ops-diagnosis.sqlite3",
        provider=MockModelProvider(),
        fault_controller=controller,
    )

    with TestClient(application) as client:
        with controller.activated("inventory_v21_latency"):
            order_response = client.post(
                "/orders",
                json=order_payload("diagnose-latency"),
            )
        incident = application.state.anomaly_detector.detect()[0]
        result = application.state.incident_workflow.investigate(
            incident.incident_id
        )

    assert order_response.status_code == 200
    assert result.incident.status is IncidentStatus.DIAGNOSED
    diagnosis = result.incident.diagnosis
    assert diagnosis is not None
    assert len(diagnosis.root_cause_candidates) == 1
    root_cause = diagnosis.root_cause_candidates[0]
    assert "v2.1" in root_cause.cause
    assert "latency regression" in root_cause.cause
    assert root_cause.confidence == pytest.approx(0.9)

    evidence_by_id = {item.evidence_id: item for item in result.evidence}
    assert {
        evidence_by_id[evidence_id].source.type
        for evidence_id in root_cause.evidence_ids
    } == {"metric", "trace", "deployment"}
    assert all(
        evidence_id in evidence_by_id
        for fact in diagnosis.facts
        for evidence_id in fact.evidence_ids
    )
    assert diagnosis.recommended_actions[0].action == "rollback"
    assert diagnosis.recommended_actions[0].requires_approval is True

    spans = application.state.trace_recorder.get_trace(result.trace_id)
    assert {
        (span.component, span.action)
        for span in spans
    }.issuperset(
        {
            ("incident-workflow", "incident.investigate"),
            ("ops-guardian-agent", "ops.plan_investigation"),
            ("query_observability", "tool.execute"),
            ("query_service_context", "tool.execute"),
            ("ops-guardian-agent", "ops.diagnose"),
            ("model-gateway", "generate.ops_guardian"),
            ("evidence-validator", "diagnosis.validate"),
            ("incident-workflow", "diagnosis.persist"),
        }
    )


def test_fabricated_evidence_id_is_rejected_before_diagnosis_persistence(
    tmp_path: Path,
) -> None:
    fake_id = "E-NOT-IN-STORE"
    incident_id = "INC-FAKE-EVIDENCE"
    provider = MockModelProvider(
        responses={
            ModelTaskType.OPS_GUARDIAN.value: {
                "incident_id": incident_id,
                "facts": [
                    {
                        "statement": "Unsupported statement",
                        "evidence_ids": [fake_id],
                    }
                ],
                "root_cause_candidates": [
                    {
                        "cause": "Unsupported root cause",
                        "confidence": 0.99,
                        "evidence_ids": [fake_id],
                    }
                ],
                "missing_information": [],
                "recommended_actions": [
                    {
                        "action": "rollback",
                        "target": "inventory-agent",
                        "risk_level": "high",
                        "requires_approval": True,
                        "evidence_ids": [fake_id],
                    }
                ],
            }
        }
    )
    application = create_app(
        database_path=tmp_path / "fake-evidence.sqlite3",
        provider=provider,
    )
    incident = IncidentRecord(
        incident_id=incident_id,
        status=IncidentStatus.OPEN,
        object_id="inventory-agent",
        rule="inventory_p95_latency",
        summary="Inventory latency requires investigation",
        occurred_at=datetime.now(timezone.utc),
        source_refs=["metric:inventory-agent.inventory.check"],
    )
    application.state.incident_repository.create(incident)
    with application.state.trace_recorder.span(
        "TRACE-FAKE-SOURCE",
        "inventory-agent",
        "inventory.check",
    ):
        pass

    with TestClient(application):
        with pytest.raises(
            UnknownEvidenceReferenceError,
            match="not stored for this incident",
        ):
            application.state.incident_workflow.investigate(
                incident_id,
                trace_id="TRACE-FAKE-DIAGNOSIS",
            )

    stored = application.state.incident_repository.get(incident_id)
    assert stored is not None
    assert stored.status is IncidentStatus.ESCALATED
    assert stored.diagnosis is None
    validation_spans = [
        span
        for span in application.state.trace_recorder.get_trace(
            "TRACE-FAKE-DIAGNOSIS"
        )
        if span.component == "evidence-validator"
    ]
    assert len(validation_spans) == 1
    assert validation_spans[0].status.value == "error"


def test_missing_evidence_produces_missing_information_and_escalation(
    tmp_path: Path,
) -> None:
    application = create_app(
        database_path=tmp_path / "missing-evidence.sqlite3",
        provider=MockModelProvider(),
    )
    incident = IncidentRecord(
        incident_id="INC-MISSING-EVIDENCE",
        status=IncidentStatus.OPEN,
        object_id="unknown-agent",
        rule="unknown_runtime_anomaly",
        summary="Unknown object requires investigation",
        occurred_at=datetime.now(timezone.utc),
        source_refs=["runtime:unknown-agent"],
    )
    application.state.incident_repository.create(incident)

    with TestClient(application):
        result = application.state.incident_workflow.investigate(
            incident.incident_id
        )

    assert result.incident.status is IncidentStatus.ESCALATED
    diagnosis = result.incident.diagnosis
    assert diagnosis is not None
    assert diagnosis.root_cause_candidates == []
    assert diagnosis.recommended_actions == []
    assert diagnosis.missing_information
    assert result.evidence == []
    assert result.observability_status.value == "no_data"
    assert result.service_context_status.value == "no_data"
