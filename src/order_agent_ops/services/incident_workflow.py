"""Evidence-first investigation workflow for operations incidents."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from order_agent_ops.agents.ops_guardian import InvestigationPlan, OpsGuardianAgent
from order_agent_ops.domain.base import DomainModel, TraceId
from order_agent_ops.domain.enums import IncidentStatus
from order_agent_ops.domain.evidence import EvidenceRecord
from order_agent_ops.domain.incidents import IncidentRecord
from order_agent_ops.ops.evidence_store import EvidenceStore
from order_agent_ops.ops.evidence_validator import (
    EvidenceValidationError,
    EvidenceValidator,
)
from order_agent_ops.storage.repositories import (
    IncidentRepository,
    RecordNotFoundError,
)
from order_agent_ops.telemetry.tracing import TraceRecorder
from order_agent_ops.tools.query_observability import (
    QueryObservabilityRequest,
    QueryObservabilityTool,
)
from order_agent_ops.tools.query_service_context import (
    QueryServiceContextRequest,
    QueryServiceContextTool,
)
from order_agent_ops.tools.schemas import QueryStatus


class IncidentInvestigationResult(DomainModel):
    trace_id: TraceId
    incident: IncidentRecord
    plan: InvestigationPlan
    evidence: list[EvidenceRecord]
    observability_status: QueryStatus
    service_context_status: QueryStatus


class IncidentWorkflow:
    """Collect read-only evidence, validate diagnosis, then persist it."""

    def __init__(
        self,
        incidents: IncidentRepository,
        evidence_store: EvidenceStore,
        ops_guardian: OpsGuardianAgent,
        evidence_validator: EvidenceValidator,
        query_observability: QueryObservabilityTool,
        query_service_context: QueryServiceContextTool,
        trace_recorder: TraceRecorder,
    ) -> None:
        self._incidents = incidents
        self._evidence_store = evidence_store
        self._ops_guardian = ops_guardian
        self._evidence_validator = evidence_validator
        self._query_observability = query_observability
        self._query_service_context = query_service_context
        self._traces = trace_recorder

    def investigate(
        self,
        incident_id: str,
        *,
        trace_id: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> IncidentInvestigationResult:
        incident = self._incidents.get(incident_id)
        if incident is None:
            raise RecordNotFoundError(f"Incident not found: {incident_id}")
        if incident.status not in {IncidentStatus.OPEN, IncidentStatus.ESCALATED}:
            raise ValueError(
                "Incident investigation requires OPEN or ESCALATED status"
            )

        active_trace_id = trace_id or self._traces.new_trace_id()
        window_start = start_time or incident.occurred_at - timedelta(minutes=10)
        window_end = end_time or datetime.now(timezone.utc) + timedelta(minutes=1)
        if window_end < window_start:
            raise ValueError("Investigation end_time must not precede start_time")

        with self._traces.span(
            active_trace_id,
            "incident-workflow",
            "incident.investigate",
        ):
            investigating = self._incidents.transition_status(
                incident.incident_id,
                IncidentStatus.INVESTIGATING,
                trigger_component="incident-workflow",
                reason="Evidence collection started",
                references=incident.source_refs,
            )
            plan = self._ops_guardian.plan_investigation(
                investigating,
                trace_id=active_trace_id,
            )

            with self._traces.span(
                active_trace_id,
                self._query_observability.name,
                "tool.execute",
            ):
                observability = self._query_observability.execute(
                    QueryObservabilityRequest(
                        object_id=plan.object_id,
                        start_time=window_start,
                        end_time=window_end,
                        data_types=plan.observability_data_types,
                    ),
                    incident_id=incident.incident_id,
                )
            with self._traces.span(
                active_trace_id,
                self._query_service_context.name,
                "tool.execute",
            ):
                service_context = self._query_service_context.execute(
                    QueryServiceContextRequest(
                        object_id=plan.object_id,
                        include=plan.service_context_includes,
                    ),
                    incident_id=incident.incident_id,
                )

            evidence = self._evidence_store.query(incident_id=incident.incident_id)
            diagnosis = self._ops_guardian.diagnose(
                investigating,
                evidence,
                trace_id=active_trace_id,
            )
            required_source_types = (
                {"metric", "trace", "deployment"}
                if incident.rule == "inventory_p95_latency"
                else set()
            )
            try:
                with self._traces.span(
                    active_trace_id,
                    "evidence-validator",
                    "diagnosis.validate",
                ):
                    self._evidence_validator.validate(
                        diagnosis,
                        incident_id=incident.incident_id,
                        required_source_types=required_source_types,
                    )
            except EvidenceValidationError:
                self._incidents.transition_status(
                    incident.incident_id,
                    IncidentStatus.ESCALATED,
                    trigger_component="evidence-validator",
                    reason="Diagnosis rejected by evidence validation",
                    references=[item.evidence_id for item in evidence],
                )
                raise

            with self._traces.span(
                active_trace_id,
                "incident-workflow",
                "diagnosis.persist",
            ):
                current = self._incidents.get(incident.incident_id)
                if current is None:  # pragma: no cover - already loaded above
                    raise RecordNotFoundError(
                        f"Incident not found: {incident.incident_id}"
                    )
                self._incidents.update(
                    current.model_copy(update={"diagnosis": diagnosis})
                )
                target_status = (
                    IncidentStatus.DIAGNOSED
                    if diagnosis.root_cause_candidates
                    else IncidentStatus.ESCALATED
                )
                final_incident = self._incidents.transition_status(
                    incident.incident_id,
                    target_status,
                    trigger_component="incident-workflow",
                    reason=(
                        "Evidence-backed diagnosis completed"
                        if target_status is IncidentStatus.DIAGNOSED
                        else "Evidence is incomplete; human investigation required"
                    ),
                    references=[item.evidence_id for item in evidence],
                )

            return IncidentInvestigationResult(
                trace_id=active_trace_id,
                incident=final_incident,
                plan=plan,
                evidence=evidence,
                observability_status=observability.status,
                service_context_status=service_context.status,
            )
