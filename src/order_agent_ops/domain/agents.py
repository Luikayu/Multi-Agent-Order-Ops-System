"""Agent and tool execution audit records."""

from pydantic import AwareDatetime, Field

from order_agent_ops.domain.base import (
    DomainModel,
    NonEmptyString,
    RunId,
    ToolCallId,
    TraceId,
)
from order_agent_ops.domain.enums import RunStatus


class AgentRunRecord(DomainModel):
    run_id: RunId
    trace_id: TraceId
    agent_name: NonEmptyString
    agent_version: NonEmptyString
    prompt_version: NonEmptyString
    model_provider: NonEmptyString
    model_name: NonEmptyString
    status: RunStatus
    started_at: AwareDatetime
    ended_at: AwareDatetime | None = None
    duration_ms: float | None = Field(default=None, ge=0)
    retry_count: int = Field(default=0, ge=0)
    error_type: str | None = None


class ToolCallRecord(DomainModel):
    call_id: ToolCallId
    trace_id: TraceId
    tool_name: NonEmptyString
    tool_version: NonEmptyString
    status: RunStatus
    started_at: AwareDatetime
    ended_at: AwareDatetime | None = None
    duration_ms: float | None = Field(default=None, ge=0)
    retry_count: int = Field(default=0, ge=0)
    error_type: str | None = None
