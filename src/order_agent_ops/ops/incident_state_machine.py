"""Deterministic incident state transitions."""

from order_agent_ops.domain.enums import IncidentStatus


class InvalidIncidentTransitionError(ValueError):
    """Raised when an incident attempts an invalid lifecycle transition."""


class IncidentStateMachine:
    """Validate investigation, remediation, and verification sequencing."""

    _transitions: dict[IncidentStatus, frozenset[IncidentStatus]] = {
        IncidentStatus.OPEN: frozenset(
            {IncidentStatus.INVESTIGATING, IncidentStatus.ESCALATED}
        ),
        IncidentStatus.INVESTIGATING: frozenset(
            {IncidentStatus.DIAGNOSED, IncidentStatus.ESCALATED}
        ),
        IncidentStatus.DIAGNOSED: frozenset(
            {IncidentStatus.REMEDIATING, IncidentStatus.ESCALATED}
        ),
        IncidentStatus.REMEDIATING: frozenset(
            {IncidentStatus.VERIFYING, IncidentStatus.ESCALATED}
        ),
        IncidentStatus.VERIFYING: frozenset(
            {IncidentStatus.RESOLVED, IncidentStatus.ESCALATED}
        ),
        IncidentStatus.RESOLVED: frozenset({IncidentStatus.CLOSED}),
        IncidentStatus.ESCALATED: frozenset(
            {IncidentStatus.INVESTIGATING, IncidentStatus.CLOSED}
        ),
        IncidentStatus.CLOSED: frozenset(),
    }

    @classmethod
    def allowed_transitions(
        cls, current: IncidentStatus
    ) -> frozenset[IncidentStatus]:
        return cls._transitions[current]

    @classmethod
    def transition(
        cls, current: IncidentStatus, target: IncidentStatus
    ) -> IncidentStatus:
        if target not in cls.allowed_transitions(current):
            raise InvalidIncidentTransitionError(
                f"Invalid incident state transition: {current.value} -> {target.value}"
            )
        return target
