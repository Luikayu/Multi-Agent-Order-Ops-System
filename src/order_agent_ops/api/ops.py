"""Operations APIs over the existing deterministic services."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, Field

from order_agent_ops.api.orders import ErrorResponse
from order_agent_ops.business.replay_service import (
    OrderNotReplayableError,
    ReplayResult,
    ReplayService,
)
from order_agent_ops.domain.approvals import RemediationAction
from order_agent_ops.domain.base import (
    ApprovalId,
    DomainModel,
    EvidenceId,
    NonEmptyString,
    TraceId,
)
from order_agent_ops.domain.enums import (
    ActionRiskLevel,
    ApprovalStatus,
)
from order_agent_ops.domain.evidence import EvidenceRecord
from order_agent_ops.domain.incidents import IncidentRecord
from order_agent_ops.domain.orders import OrderRecord
from order_agent_ops.faults import FaultController, FaultTarget
from order_agent_ops.models.errors import ModelGatewayError
from order_agent_ops.models.gateway import ModelGateway
from order_agent_ops.ops.approval_service import (
    ApprovalDecisionResult,
    ApprovalError,
    ApprovalService,
)
from order_agent_ops.ops.detector import AnomalyDetector
from order_agent_ops.ops.evidence_store import EvidenceStore
from order_agent_ops.ops.evidence_validator import EvidenceValidationError
from order_agent_ops.ops.remediation_executor import ExecuteRemediationRequest
from order_agent_ops.ops.verification_service import (
    VerificationResult,
    VerificationService,
)
from order_agent_ops.services.incident_workflow import (
    IncidentInvestigationResult,
    IncidentWorkflow,
)
from order_agent_ops.storage.repositories import (
    ApprovalRepository,
    IncidentRepository,
    OrderRepository,
    RecordNotFoundError,
    RemediationActionRepository,
)
from order_agent_ops.telemetry.tracing import SpanRecord, TraceRecorder
from order_agent_ops.tools.execute_remediation import ExecuteRemediationTool


class OpsCounts(DomainModel):
    orders: int = Field(ge=0)
    incidents: int = Field(ge=0)
    evidence: int = Field(ge=0)
    approvals: int = Field(ge=0)
    actions: int = Field(ge=0)


class OpsHealthResponse(DomainModel):
    status: NonEmptyString
    model_provider: NonEmptyString
    model_name: NonEmptyString
    active_faults: dict[str, str]
    counts: OpsCounts


class IncidentDetailResponse(DomainModel):
    incident: IncidentRecord
    evidence: list[EvidenceRecord]


class ApprovalCreateRequest(DomainModel):
    action: NonEmptyString
    target: NonEmptyString
    target_version: str | None = None
    risk_level: ActionRiskLevel
    impact_scope: NonEmptyString
    reason_evidence_ids: list[EvidenceId] = Field(min_length=1)
    requested_by: NonEmptyString


class ApprovalDecisionRequest(DomainModel):
    approved: bool
    decided_by: NonEmptyString


class ApprovalView(DomainModel):
    approval_id: ApprovalId
    action: NonEmptyString
    target: NonEmptyString
    target_version: str | None = None
    risk_level: ActionRiskLevel
    requires_approval: bool
    impact_scope: NonEmptyString
    reason_evidence_ids: list[EvidenceId]
    status: ApprovalStatus
    requested_by: NonEmptyString
    requested_at: AwareDatetime
    decided_by: str | None = None
    decided_at: AwareDatetime | None = None
    token_expires_at: AwareDatetime | None = None
    token_used: bool

    @classmethod
    def from_record(cls, approval) -> "ApprovalView":
        return cls(
            approval_id=approval.approval_id,
            action=approval.action,
            target=approval.target,
            target_version=approval.target_version,
            risk_level=approval.risk_level,
            requires_approval=approval.requires_approval,
            impact_scope=approval.impact_scope,
            reason_evidence_ids=approval.reason_evidence_ids,
            status=approval.status,
            requested_by=approval.requested_by,
            requested_at=approval.requested_at,
            decided_by=approval.decided_by,
            decided_at=approval.decided_at,
            token_expires_at=approval.token_expires_at,
            token_used=approval.token_used_at is not None,
        )


class ApprovalDecisionResponse(DomainModel):
    approval: ApprovalView
    approval_token: str | None = None


class ReplayRequest(DomainModel):
    approval_id: ApprovalId | None = None
    approval_token: str | None = None
    trace_id: TraceId | None = None


class TraceResponse(DomainModel):
    trace_id: TraceId
    spans: list[SpanRecord]


class FaultScenarioView(DomainModel):
    name: NonEmptyString
    target: NonEmptyString
    component_version: NonEmptyString
    delay_ms: int = Field(ge=0)
    error_rate: float = Field(ge=0, le=1)
    force_timeout: bool
    invalid_output: bool
    active: bool


class FaultUpdateResponse(DomainModel):
    scenario: FaultScenarioView
    message: NonEmptyString


@dataclass(frozen=True, slots=True)
class OpsApiDependencies:
    orders: OrderRepository
    incidents: IncidentRepository
    approvals: ApprovalRepository
    actions: RemediationActionRepository
    evidence: EvidenceStore
    traces: TraceRecorder
    gateway: ModelGateway
    faults: FaultController
    detector: AnomalyDetector
    incident_workflow: IncidentWorkflow
    approval_service: ApprovalService
    execute_remediation: ExecuteRemediationTool
    verification_service: VerificationService
    replay_service: ReplayService


def _error(
    status_code: int,
    error_code: str,
    message: str,
    *,
    trace_id: str | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=ErrorResponse(
            error_code=error_code,
            message=message,
            trace_id=trace_id,
        ).model_dump(mode="json"),
    )


def _fault_view(
    name: str,
    faults: FaultController,
) -> FaultScenarioView:
    scenario = faults.scenarios[name]
    return FaultScenarioView(
        name=name,
        target=scenario.target.value,
        component_version=scenario.component_version,
        delay_ms=scenario.delay_ms,
        error_rate=scenario.error_rate,
        force_timeout=scenario.force_timeout,
        invalid_output=scenario.invalid_output,
        active=faults.active_scenario_name(scenario.target) == name,
    )


def create_ops_router(dependencies: OpsApiDependencies) -> APIRouter:
    router = APIRouter(tags=["operations"])

    @router.get("/ops/health", response_model=OpsHealthResponse)
    def health() -> OpsHealthResponse:
        active_faults = {
            target.value: scenario_name
            for target in FaultTarget
            if (scenario_name := dependencies.faults.active_scenario_name(target))
            is not None
        }
        return OpsHealthResponse(
            status="ok",
            model_provider=dependencies.gateway.provider.provider_name,
            model_name=dependencies.gateway.provider.model_name,
            active_faults=active_faults,
            counts=OpsCounts(
                orders=len(dependencies.orders.list_all()),
                incidents=len(dependencies.incidents.list_all()),
                evidence=len(dependencies.evidence.query()),
                approvals=len(dependencies.approvals.list_all()),
                actions=len(dependencies.actions.list_all()),
            ),
        )

    @router.get("/ops/orders", response_model=list[OrderRecord])
    def list_orders() -> list[OrderRecord]:
        return dependencies.orders.list_all()

    @router.get("/ops/incidents", response_model=list[IncidentRecord])
    def list_incidents() -> list[IncidentRecord]:
        return dependencies.incidents.list_all()

    @router.get(
        "/ops/incidents/{incident_id}",
        response_model=IncidentDetailResponse,
        responses={404: {"model": ErrorResponse}},
    )
    def get_incident(
        incident_id: str,
    ) -> IncidentDetailResponse | JSONResponse:
        incident = dependencies.incidents.get(incident_id)
        if incident is None:
            return _error(404, "INCIDENT_NOT_FOUND", f"Incident not found: {incident_id}")
        return IncidentDetailResponse(
            incident=incident,
            evidence=dependencies.evidence.query(incident_id=incident_id),
        )

    @router.post(
        "/ops/incidents/{incident_id}/diagnose",
        response_model=IncidentInvestigationResult,
    )
    def diagnose_incident(
        incident_id: str,
    ) -> IncidentInvestigationResult | JSONResponse:
        try:
            return dependencies.incident_workflow.investigate(incident_id)
        except RecordNotFoundError as error:
            return _error(404, "INCIDENT_NOT_FOUND", str(error))
        except EvidenceValidationError as error:
            return _error(422, "DIAGNOSIS_EVIDENCE_INVALID", str(error))
        except ModelGatewayError as error:
            return _error(502, "DIAGNOSIS_MODEL_ERROR", str(error))
        except ValueError as error:
            return _error(409, "INCIDENT_NOT_DIAGNOSABLE", str(error))

    @router.post(
        "/ops/incidents/{incident_id}/verify",
        response_model=VerificationResult,
    )
    def verify_incident(
        incident_id: str,
    ) -> VerificationResult | JSONResponse:
        try:
            return dependencies.verification_service.verify(incident_id)
        except RecordNotFoundError as error:
            return _error(404, "INCIDENT_NOT_FOUND", str(error))
        except ValueError as error:
            return _error(409, "INCIDENT_NOT_VERIFIABLE", str(error))

    @router.get("/ops/evidence", response_model=list[EvidenceRecord])
    def list_evidence(
        incident_id: str | None = None,
        object_id: str | None = None,
    ) -> list[EvidenceRecord]:
        return dependencies.evidence.query(
            incident_id=incident_id,
            object_id=object_id,
        )

    @router.get("/ops/approvals", response_model=list[ApprovalView])
    def list_approvals() -> list[ApprovalView]:
        return [
            ApprovalView.from_record(approval)
            for approval in dependencies.approvals.list_all()
        ]

    @router.post("/ops/approvals", response_model=ApprovalView)
    def create_approval(
        request: ApprovalCreateRequest,
    ) -> ApprovalView | JSONResponse:
        try:
            approval = dependencies.approval_service.create_request(
                **request.model_dump()
            )
        except ValueError as error:
            return _error(422, "APPROVAL_EVIDENCE_INVALID", str(error))
        return ApprovalView.from_record(approval)

    @router.post(
        "/ops/approvals/{approval_id}/decision",
        response_model=ApprovalDecisionResponse,
    )
    def decide_approval(
        approval_id: str,
        request: ApprovalDecisionRequest,
    ) -> ApprovalDecisionResponse | JSONResponse:
        try:
            result: ApprovalDecisionResult = dependencies.approval_service.decide(
                approval_id,
                approved=request.approved,
                decided_by=request.decided_by,
            )
        except RecordNotFoundError as error:
            return _error(404, "APPROVAL_NOT_FOUND", str(error))
        except ApprovalError as error:
            return _error(409, error.error_code, str(error))
        return ApprovalDecisionResponse(
            approval=ApprovalView.from_record(result.approval),
            approval_token=result.approval_token,
        )

    @router.get("/ops/actions", response_model=list[RemediationAction])
    def list_actions() -> list[RemediationAction]:
        return dependencies.actions.list_all()

    @router.post("/ops/actions", response_model=RemediationAction)
    def execute_action(request: ExecuteRemediationRequest) -> RemediationAction:
        return dependencies.execute_remediation.execute(request)

    @router.get(
        "/ops/traces/{trace_id}",
        response_model=TraceResponse,
        responses={404: {"model": ErrorResponse}},
    )
    def get_trace(trace_id: str) -> TraceResponse | JSONResponse:
        spans = dependencies.traces.get_trace(trace_id)
        if not spans:
            return _error(404, "TRACE_NOT_FOUND", f"Trace not found: {trace_id}")
        return TraceResponse(trace_id=trace_id, spans=spans)

    @router.post("/ops/detect", response_model=list[IncidentRecord])
    def detect_anomalies() -> list[IncidentRecord]:
        return dependencies.detector.detect()

    @router.get("/ops/faults", response_model=list[FaultScenarioView])
    def list_faults() -> list[FaultScenarioView]:
        return [
            _fault_view(name, dependencies.faults)
            for name in sorted(dependencies.faults.scenarios)
        ]

    @router.post(
        "/ops/faults/{scenario_name}/activate",
        response_model=FaultUpdateResponse,
    )
    def activate_fault(scenario_name: str) -> FaultUpdateResponse | JSONResponse:
        try:
            dependencies.faults.activate(scenario_name)
        except KeyError as error:
            return _error(404, "FAULT_SCENARIO_NOT_FOUND", str(error))
        return FaultUpdateResponse(
            scenario=_fault_view(scenario_name, dependencies.faults),
            message=f"Fault scenario activated: {scenario_name}",
        )

    @router.post(
        "/ops/faults/{scenario_name}/deactivate",
        response_model=FaultUpdateResponse,
    )
    def deactivate_fault(scenario_name: str) -> FaultUpdateResponse | JSONResponse:
        try:
            dependencies.faults.deactivate(scenario_name)
        except KeyError as error:
            return _error(404, "FAULT_SCENARIO_NOT_FOUND", str(error))
        return FaultUpdateResponse(
            scenario=_fault_view(scenario_name, dependencies.faults),
            message=f"Fault scenario deactivated: {scenario_name}",
        )

    @router.post(
        "/orders/{order_id}/replay",
        response_model=ReplayResult,
    )
    def replay_order(
        order_id: str,
        request: ReplayRequest,
    ) -> ReplayResult | JSONResponse:
        try:
            return dependencies.replay_service.replay(
                order_id,
                approval_id=request.approval_id,
                approval_token=request.approval_token,
                trace_id=request.trace_id,
            )
        except RecordNotFoundError as error:
            return _error(404, "ORDER_NOT_FOUND", str(error))
        except OrderNotReplayableError as error:
            return _error(409, "ORDER_NOT_REPLAYABLE", str(error))
        except ApprovalError as error:
            return _error(403, error.error_code, str(error))

    return router
