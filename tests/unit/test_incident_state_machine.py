import pytest

from order_agent_ops.domain.enums import IncidentStatus
from order_agent_ops.ops.incident_state_machine import (
    IncidentStateMachine,
    InvalidIncidentTransitionError,
)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (IncidentStatus.OPEN, IncidentStatus.INVESTIGATING),
        (IncidentStatus.INVESTIGATING, IncidentStatus.DIAGNOSED),
        (IncidentStatus.DIAGNOSED, IncidentStatus.REMEDIATING),
        (IncidentStatus.REMEDIATING, IncidentStatus.VERIFYING),
        (IncidentStatus.VERIFYING, IncidentStatus.RESOLVED),
        (IncidentStatus.RESOLVED, IncidentStatus.CLOSED),
        (IncidentStatus.OPEN, IncidentStatus.ESCALATED),
        (IncidentStatus.ESCALATED, IncidentStatus.INVESTIGATING),
        (IncidentStatus.ESCALATED, IncidentStatus.CLOSED),
    ],
)
def test_incident_lifecycle_transitions_are_allowed(
    current: IncidentStatus, target: IncidentStatus
) -> None:
    assert IncidentStateMachine.transition(current, target) is target


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (IncidentStatus.OPEN, IncidentStatus.RESOLVED),
        (IncidentStatus.INVESTIGATING, IncidentStatus.REMEDIATING),
        (IncidentStatus.DIAGNOSED, IncidentStatus.RESOLVED),
        (IncidentStatus.CLOSED, IncidentStatus.OPEN),
    ],
)
def test_invalid_incident_transitions_raise_business_error(
    current: IncidentStatus, target: IncidentStatus
) -> None:
    with pytest.raises(
        InvalidIncidentTransitionError,
        match=f"{current.value} -> {target.value}",
    ):
        IncidentStateMachine.transition(current, target)
