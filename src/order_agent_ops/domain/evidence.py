"""Evidence contracts with exactly five required top-level fields."""

from typing import Any

from pydantic import AwareDatetime, ConfigDict, model_validator

from order_agent_ops.domain.base import DomainModel, EvidenceId, NonEmptyString


class EvidenceTimeWindow(DomainModel):
    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def end_must_not_precede_start(self) -> "EvidenceTimeWindow":
        if self.end < self.start:
            raise ValueError("Evidence time window end must not precede start")
        return self


class EvidenceSource(DomainModel):
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    type: NonEmptyString
    query_ref: NonEmptyString


class EvidenceObject(DomainModel):
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    id: NonEmptyString
    version: str | None = None


class EvidenceFact(DomainModel):
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    observation: NonEmptyString


class EvidenceRecord(DomainModel):
    evidence_id: EvidenceId
    time: AwareDatetime | EvidenceTimeWindow
    source: EvidenceSource
    object: EvidenceObject
    fact: EvidenceFact
