"""Incident and evidence-backed diagnosis contracts."""

from pydantic import AwareDatetime, Field, model_validator

from order_agent_ops.domain.base import (
    DomainModel,
    EvidenceId,
    IncidentId,
    NonEmptyString,
)
from order_agent_ops.domain.enums import ActionRiskLevel, IncidentStatus


class DiagnosisFact(DomainModel):
    statement: NonEmptyString
    evidence_ids: list[EvidenceId] = Field(min_length=1)


class RootCauseCandidate(DomainModel):
    cause: NonEmptyString
    confidence: float = Field(ge=0, le=1)
    evidence_ids: list[EvidenceId] = Field(min_length=1)


class RecommendedAction(DomainModel):
    action: NonEmptyString
    target: NonEmptyString
    risk_level: ActionRiskLevel
    requires_approval: bool
    evidence_ids: list[EvidenceId] = Field(min_length=1)

    @model_validator(mode="after")
    def high_risk_requires_approval(self) -> "RecommendedAction":
        if self.risk_level in {ActionRiskLevel.HIGH, ActionRiskLevel.CRITICAL}:
            if not self.requires_approval:
                raise ValueError("High-risk actions must require approval")
        return self


class DiagnosisResult(DomainModel):
    incident_id: IncidentId
    facts: list[DiagnosisFact]
    root_cause_candidates: list[RootCauseCandidate]
    missing_information: list[NonEmptyString]
    recommended_actions: list[RecommendedAction]


class IncidentRecord(DomainModel):
    incident_id: IncidentId
    status: IncidentStatus
    object_id: NonEmptyString
    rule: NonEmptyString
    summary: NonEmptyString
    occurred_at: AwareDatetime
    source_refs: list[NonEmptyString] = Field(default_factory=list)
    diagnosis: DiagnosisResult | None = None
