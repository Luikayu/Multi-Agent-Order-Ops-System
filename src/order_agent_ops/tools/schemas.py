"""Shared response contracts for read-only operations tools."""

from enum import StrEnum

from pydantic import Field

from order_agent_ops.domain.base import DomainModel, NonEmptyString
from order_agent_ops.domain.evidence import EvidenceRecord


class QueryStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    NO_DATA = "no_data"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    INVALID_REQUEST = "invalid_request"


class QueryIssue(DomainModel):
    source: NonEmptyString
    error_code: NonEmptyString
    message: NonEmptyString


class EvidenceQueryResult(DomainModel):
    status: QueryStatus
    query_ref: NonEmptyString
    evidence: list[EvidenceRecord]
    issues: list[QueryIssue] = Field(default_factory=list)
