from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from order_agent_ops.domain.enums import IncidentStatus
from order_agent_ops.domain.incidents import IncidentRecord
from order_agent_ops.faults import FaultController
from order_agent_ops.main import create_app
from order_agent_ops.models import MockModelProvider
from order_agent_ops.ops.evidence_store import EvidenceDraft, EvidenceStore
from order_agent_ops.storage.database import Database
from order_agent_ops.storage.repositories import IncidentRepository
from order_agent_ops.tools.query_observability import (
    ObservabilityDataType,
    QueryObservabilityRequest,
    QueryObservabilityTool,
)
from order_agent_ops.tools.query_service_context import (
    QueryServiceContextRequest,
    ServiceContextInclude,
)
from order_agent_ops.tools.schemas import QueryStatus


NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


def order_payload(key: str) -> dict[str, object]:
    return {
        "user_id": "USER-001",
        "items": [
            {"sku": "SKU-001", "quantity": 1, "unit_price": "399.00"}
        ],
        "total_amount": "399.00",
        "idempotency_key": key,
    }


def test_inventory_v21_observability_and_deployment_queries_persist_evidence(
    tmp_path: Path,
) -> None:
    controller = FaultController.from_yaml()
    application = create_app(
        database_path=tmp_path / "ops-query.sqlite3",
        provider=MockModelProvider(),
        fault_controller=controller,
    )
    started_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    with TestClient(application) as client:
        with controller.activated("inventory_v21_latency"):
            response = client.post("/orders", json=order_payload("ops-evidence"))
        ended_at = datetime.now(timezone.utc) + timedelta(seconds=1)
        incident = application.state.anomaly_detector.detect()[0]

        observability = application.state.query_observability.execute(
            QueryObservabilityRequest(
                object_id="inventory-agent",
                start_time=started_at,
                end_time=ended_at,
                data_types=["metrics", "logs", "traces"],
                filters={"trace_id": response.json()["trace_id"]},
            ),
            incident_id=incident.incident_id,
        )
        context = application.state.query_service_context.execute(
            QueryServiceContextRequest(
                object_id="inventory-agent",
                include=[
                    "deployments",
                    "configuration",
                    "dependencies",
                    "owner",
                    "runbook",
                ],
            ),
            incident_id=incident.incident_id,
        )

    assert observability.status is QueryStatus.SUCCESS
    assert {item.source.type for item in observability.evidence} == {
        "metric",
        "log",
        "trace",
    }
    assert all(item.object.id == "inventory-agent" for item in observability.evidence)
    assert all(item.object.version == "v2.1" for item in observability.evidence)
    assert all(
        set(item.model_dump()) == {"evidence_id", "time", "source", "object", "fact"}
        for item in observability.evidence
    )
    metric = next(
        item for item in observability.evidence if item.source.type == "metric"
    )
    assert metric.fact.model_extra["p95_duration_ms"] >= 2200

    assert context.status is QueryStatus.SUCCESS
    assert {
        "deployment",
        "configuration",
        "dependency",
        "owner",
        "runbook",
    }.issubset({item.source.type for item in context.evidence})
    v21_deployment = [
        item
        for item in context.evidence
        if item.source.type == "deployment" and item.object.version == "v2.1"
    ]
    assert len(v21_deployment) == 1

    linked = application.state.evidence_store.query(
        incident_id=incident.incident_id
    )
    by_object = application.state.evidence_store.query(object_id="inventory-agent")
    by_time = application.state.evidence_store.query(
        start_time=started_at,
        end_time=ended_at,
    )
    assert {item.evidence_id for item in linked} == {
        item.evidence_id for item in [*observability.evidence, *context.evidence]
    }
    assert len(by_object) == len(linked)
    assert {item.evidence_id for item in observability.evidence}.issubset(
        {item.evidence_id for item in by_time}
    )


def test_no_data_query_does_not_fabricate_or_store_evidence(tmp_path: Path) -> None:
    application = create_app(
        database_path=tmp_path / "no-data.sqlite3",
        provider=MockModelProvider(),
    )
    future = datetime.now(timezone.utc) + timedelta(days=1)

    result = application.state.query_observability.execute(
        QueryObservabilityRequest(
            object_id="inventory-agent",
            start_time=future,
            end_time=future + timedelta(minutes=5),
            data_types=["metrics", "logs", "traces"],
        )
    )

    assert result.status is QueryStatus.NO_DATA
    assert result.evidence == []
    assert result.issues[0].error_code == "NO_DATA"
    assert application.state.evidence_store.query() == []
    application.state.model_gateway.close()


def test_evidence_store_generates_ids_and_queries_incident_object_and_time(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "evidence-store.sqlite3")
    database.initialize()
    incident = IncidentRecord(
        incident_id="INC-EVIDENCE-001",
        status=IncidentStatus.OPEN,
        object_id="inventory-agent",
        rule="inventory_p95_latency",
        summary="Inventory latency exceeded threshold",
        occurred_at=NOW,
        source_refs=["metric:inventory-agent.inventory.check"],
    )
    IncidentRepository(database).create(incident)
    store = EvidenceStore(database)
    draft = EvidenceDraft(
        time=NOW,
        source={"type": "metric", "query_ref": "OBS-001:metrics"},
        object={"id": "inventory-agent", "version": "v2.1"},
        fact={"observation": "P95 latency was 2300ms", "value": 2300},
    )

    first = store.save(draft, incident_id=incident.incident_id)
    second = store.save(draft)

    assert first.evidence_id.startswith("E-")
    assert second.evidence_id.startswith("E-")
    assert first.evidence_id != second.evidence_id
    assert store.query(incident_id=incident.incident_id) == [first]
    assert store.query(object_id="inventory-agent") == [first, second]
    assert store.query(
        start_time=NOW - timedelta(seconds=1),
        end_time=NOW + timedelta(seconds=1),
    ) == [first, second]

    with pytest.raises(ValidationError):
        EvidenceDraft(
            evidence_id="E-MODEL-CONTROLLED",
            **draft.model_dump(),
        )


class PartiallyFailingObservabilitySource:
    def query(self, data_type, request, *, query_ref):
        if data_type is ObservabilityDataType.LOGS:
            raise TimeoutError("simulated log timeout")
        return [
            EvidenceDraft(
                time=request.start_time,
                source={"type": "metric", "query_ref": f"{query_ref}:metrics"},
                object={"id": request.object_id, "version": "v2.1"},
                fact={"observation": "P95 latency was 2300ms"},
            )
        ]


class UnavailableObservabilitySource:
    def query(self, data_type, request, *, query_ref):
        raise RuntimeError("simulated source outage")


class TimeoutObservabilitySource:
    def query(self, data_type, request, *, query_ref):
        raise TimeoutError("simulated source timeout")


def test_observability_query_reports_partial_timeout_and_unavailable(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "query-failures.sqlite3")
    database.initialize()
    store = EvidenceStore(database)
    request = QueryObservabilityRequest(
        object_id="inventory-agent",
        start_time=NOW,
        end_time=NOW + timedelta(minutes=5),
        data_types=["metrics", "logs"],
    )

    partial = QueryObservabilityTool(
        PartiallyFailingObservabilitySource(), store
    ).execute(request)
    unavailable = QueryObservabilityTool(
        UnavailableObservabilitySource(), store
    ).execute(request)
    timed_out = QueryObservabilityTool(TimeoutObservabilitySource(), store).execute(
        request
    )

    assert partial.status is QueryStatus.PARTIAL
    assert len(partial.evidence) == 1
    assert partial.issues[0].error_code == "QUERY_TIMEOUT"
    assert unavailable.status is QueryStatus.UNAVAILABLE
    assert unavailable.evidence == []
    assert all(
        issue.error_code == "DATA_SOURCE_UNAVAILABLE"
        for issue in unavailable.issues
    )
    assert timed_out.status is QueryStatus.TIMEOUT
    assert timed_out.evidence == []
    assert all(issue.error_code == "QUERY_TIMEOUT" for issue in timed_out.issues)


def test_large_time_range_and_unknown_service_have_explicit_status(
    tmp_path: Path,
) -> None:
    application = create_app(
        database_path=tmp_path / "invalid-query.sqlite3",
        provider=MockModelProvider(),
    )
    too_large = application.state.query_observability.execute(
        QueryObservabilityRequest(
            object_id="inventory-agent",
            start_time=NOW,
            end_time=NOW + timedelta(days=2),
            data_types=["metrics"],
        )
    )
    missing = application.state.query_service_context.execute(
        QueryServiceContextRequest(
            object_id="unknown-agent",
            include=["deployments", "owner"],
        )
    )

    assert too_large.status is QueryStatus.INVALID_REQUEST
    assert too_large.issues[0].error_code == "TIME_RANGE_TOO_LARGE"
    assert missing.status is QueryStatus.NO_DATA
    assert missing.evidence == []
    assert {issue.error_code for issue in missing.issues} == {
        "DEPLOYMENT_NOT_FOUND",
        "OBJECT_NOT_FOUND",
    }
    application.state.model_gateway.close()


def test_observability_request_rejects_invalid_parameters() -> None:
    with pytest.raises(ValidationError, match="end_time"):
        QueryObservabilityRequest(
            object_id="inventory-agent",
            start_time=NOW,
            end_time=NOW - timedelta(seconds=1),
            data_types=["metrics"],
        )

    with pytest.raises(ValidationError, match="duplicates"):
        QueryObservabilityRequest(
            object_id="inventory-agent",
            start_time=NOW,
            end_time=NOW + timedelta(seconds=1),
            data_types=["metrics", "metrics"],
        )
