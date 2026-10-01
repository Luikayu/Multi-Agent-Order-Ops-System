from datetime import datetime, timezone
from pathlib import Path

import pytest

from order_agent_ops.domain.enums import IncidentStatus
from order_agent_ops.domain.incidents import DiagnosisResult, IncidentRecord
from order_agent_ops.ops.evidence_store import EvidenceDraft, EvidenceStore
from order_agent_ops.ops.evidence_validator import (
    EvidenceValidator,
    InsufficientEvidenceError,
)
from order_agent_ops.storage.database import Database
from order_agent_ops.storage.repositories import IncidentRepository


NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


def metric_evidence_for_incident(
    tmp_path: Path,
) -> tuple[EvidenceValidator, str, str]:
    database = Database(tmp_path / "validator.sqlite3")
    database.initialize()
    incident_id = "INC-VALIDATOR-001"
    IncidentRepository(database).create(
        IncidentRecord(
            incident_id=incident_id,
            status=IncidentStatus.INVESTIGATING,
            object_id="inventory-agent",
            rule="inventory_p95_latency",
            summary="Inventory latency exceeded threshold",
            occurred_at=NOW,
            source_refs=["metric:inventory-agent.inventory.check"],
        )
    )
    store = EvidenceStore(database)
    evidence = store.save(
        EvidenceDraft(
            time=NOW,
            source={"type": "metric", "query_ref": "OBS-001:metrics"},
            object={"id": "inventory-agent", "version": "v2.1"},
            fact={"observation": "P95 latency was 2300ms"},
        ),
        incident_id=incident_id,
    )
    return EvidenceValidator(store), incident_id, evidence.evidence_id


def diagnosis(
    incident_id: str,
    evidence_id: str,
    *,
    confidence: float,
) -> DiagnosisResult:
    return DiagnosisResult(
        incident_id=incident_id,
        facts=[
            {
                "statement": "Inventory P95 latency was elevated",
                "evidence_ids": [evidence_id],
            }
        ],
        root_cause_candidates=[
            {
                "cause": "Potential inventory regression",
                "confidence": confidence,
                "evidence_ids": [evidence_id],
            }
        ],
        missing_information=(
            ["trace and deployment evidence"] if confidence <= 0.5 else []
        ),
        recommended_actions=[],
    )


def test_high_confidence_root_cause_requires_independent_evidence_types(
    tmp_path: Path,
) -> None:
    validator, incident_id, evidence_id = metric_evidence_for_incident(tmp_path)

    with pytest.raises(InsufficientEvidenceError, match="deployment, trace"):
        validator.validate(
            diagnosis(incident_id, evidence_id, confidence=0.9),
            incident_id=incident_id,
            required_source_types={"metric", "trace", "deployment"},
        )


def test_incomplete_evidence_may_only_support_low_confidence_candidate(
    tmp_path: Path,
) -> None:
    validator, incident_id, evidence_id = metric_evidence_for_incident(tmp_path)
    result = diagnosis(incident_id, evidence_id, confidence=0.4)

    assert validator.validate(
        result,
        incident_id=incident_id,
        required_source_types={"metric", "trace", "deployment"},
    ) == result
