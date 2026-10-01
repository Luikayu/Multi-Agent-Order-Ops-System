"""Shared strict types used by domain schemas."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints


class DomainModel(BaseModel):
    """Reject unknown fields so persisted contracts remain auditable."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


NonEmptyString = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]
RequestId = Annotated[str, StringConstraints(pattern=r"^REQ-[A-Za-z0-9][A-Za-z0-9._-]*$")]
OrderId = Annotated[str, StringConstraints(pattern=r"^ORD-[A-Za-z0-9][A-Za-z0-9._-]*$")]
RunId = Annotated[str, StringConstraints(pattern=r"^RUN-[A-Za-z0-9][A-Za-z0-9._-]*$")]
TraceId = Annotated[str, StringConstraints(pattern=r"^TRACE-[A-Za-z0-9][A-Za-z0-9._-]*$")]
IncidentId = Annotated[str, StringConstraints(pattern=r"^INC-[A-Za-z0-9][A-Za-z0-9._-]*$")]
EvidenceId = Annotated[str, StringConstraints(pattern=r"^E-[A-Za-z0-9][A-Za-z0-9._-]*$")]
ApprovalId = Annotated[
    str,
    StringConstraints(pattern=r"^APPROVAL-[A-Za-z0-9][A-Za-z0-9._-]*$"),
]
ActionId = Annotated[str, StringConstraints(pattern=r"^ACTION-[A-Za-z0-9][A-Za-z0-9._-]*$")]
ToolCallId = Annotated[str, StringConstraints(pattern=r"^CALL-[A-Za-z0-9][A-Za-z0-9._-]*$")]
IntentId = Annotated[
    str,
    StringConstraints(pattern=r"^INTENT-[A-Za-z0-9][A-Za-z0-9._-]*$"),
]
