"""Finite domain states; arbitrary status strings are not accepted."""

from enum import StrEnum


class OrderStatus(StrEnum):
    RECEIVED = "RECEIVED"
    CHECKING = "CHECKING"
    APPROVED = "APPROVED"
    PENDING = "PENDING"
    REPLAYING = "REPLAYING"
    CREATING = "CREATING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    REJECTED = "REJECTED"


class IncidentStatus(StrEnum):
    OPEN = "OPEN"
    INVESTIGATING = "INVESTIGATING"
    DIAGNOSED = "DIAGNOSED"
    REMEDIATING = "REMEDIATING"
    VERIFYING = "VERIFYING"
    RESOLVED = "RESOLVED"
    ESCALATED = "ESCALATED"
    CLOSED = "CLOSED"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    UNKNOWN = "unknown"


class ActionRiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class InventoryStatus(StrEnum):
    SUFFICIENT = "sufficient"
    INSUFFICIENT = "insufficient"
    UNKNOWN = "unknown"
    TIMEOUT = "timeout"


class PurchaseIntentStatus(StrEnum):
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    SEARCHING = "SEARCHING"
    READY_FOR_CONFIRMATION = "READY_FOR_CONFIRMATION"
    CONFIRMED = "CONFIRMED"
    EXPIRED = "EXPIRED"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"
    TIMEOUT = "timeout"
    INVALID_OUTPUT = "invalid_output"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class RemediationStatus(StrEnum):
    PENDING = "pending"
    REJECTED = "rejected"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
