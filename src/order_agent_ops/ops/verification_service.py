"""Post-remediation probes and deterministic incident resolution."""

from __future__ import annotations

import math
from collections.abc import Callable

from pydantic import Field

from order_agent_ops.domain.base import DomainModel, NonEmptyString, TraceId
from order_agent_ops.domain.enums import IncidentStatus
from order_agent_ops.domain.incidents import IncidentRecord
from order_agent_ops.storage.repositories import (
    IncidentRepository,
    RecordNotFoundError,
)
from order_agent_ops.telemetry.tracing import TraceRecorder


class VerificationProbeResult(DomainModel):
    trace_id: TraceId
    success: bool
    inventory_latency_ms: float = Field(ge=0)
    order_latency_ms: float = Field(ge=0)
    detail: NonEmptyString


class VerificationResult(DomainModel):
    trace_id: TraceId
    success: bool
    inventory_p95_ms: float | None = Field(default=None, ge=0)
    order_p95_ms: float | None = Field(default=None, ge=0)
    reason: NonEmptyString
    incident: IncidentRecord


VerificationProbe = Callable[[str], VerificationProbeResult]


class VerificationService:
    """Resolve an incident only when fresh post-action probes pass."""

    def __init__(
        self,
        incidents: IncidentRepository,
        probe: VerificationProbe,
        traces: TraceRecorder,
        *,
        inventory_latency_threshold_ms: float = 2000,
        order_latency_threshold_ms: float = 2000,
        sample_count: int = 1,
    ) -> None:
        if inventory_latency_threshold_ms <= 0 or order_latency_threshold_ms <= 0:
            raise ValueError("Verification latency thresholds must be positive")
        if sample_count <= 0:
            raise ValueError("Verification sample_count must be positive")
        self._incidents = incidents
        self._probe = probe
        self._traces = traces
        self._inventory_threshold = inventory_latency_threshold_ms
        self._order_threshold = order_latency_threshold_ms
        self._sample_count = sample_count

    @staticmethod
    def _p95(values: list[float]) -> float:
        ordered = sorted(values)
        return ordered[max(1, math.ceil(0.95 * len(ordered))) - 1]

    def verify(
        self,
        incident_id: str,
        *,
        trace_id: str | None = None,
    ) -> VerificationResult:
        incident = self._incidents.get(incident_id)
        if incident is None:
            raise RecordNotFoundError(f"Incident not found: {incident_id}")
        if incident.status is not IncidentStatus.REMEDIATING:
            raise ValueError("Verification requires a REMEDIATING incident")

        active_trace_id = trace_id or self._traces.new_trace_id()
        with self._traces.span(
            active_trace_id,
            "verification-service",
            "remediation.verify",
        ):
            self._incidents.transition_status(
                incident_id,
                IncidentStatus.VERIFYING,
                trigger_component="verification-service",
                reason="Post-remediation verification started",
            )
            probes: list[VerificationProbeResult] = []
            probe_error: Exception | None = None
            try:
                for _ in range(self._sample_count):
                    probes.append(self._probe(active_trace_id))
            except Exception as error:
                probe_error = error

            inventory_p95 = (
                self._p95([item.inventory_latency_ms for item in probes])
                if probes
                else None
            )
            order_p95 = (
                self._p95([item.order_latency_ms for item in probes])
                if probes
                else None
            )
            success = (
                probe_error is None
                and len(probes) == self._sample_count
                and all(item.success for item in probes)
                and inventory_p95 is not None
                and inventory_p95 <= self._inventory_threshold
                and order_p95 is not None
                and order_p95 <= self._order_threshold
            )
            if success:
                reason = (
                    f"Verification passed: inventory P95 {inventory_p95:.1f}ms, "
                    f"order P95 {order_p95:.1f}ms"
                )
                target = IncidentStatus.RESOLVED
            else:
                if probe_error is not None:
                    reason = (
                        "Verification probe failed with "
                        f"{type(probe_error).__name__}; human investigation required"
                    )
                else:
                    reason = (
                        "Verification did not meet health and latency thresholds; "
                        "diagnosis remains unverified"
                    )
                target = IncidentStatus.ESCALATED

            final_incident = self._incidents.transition_status(
                incident_id,
                target,
                trigger_component="verification-service",
                reason=reason,
                references=[item.trace_id for item in probes],
            )
            return VerificationResult(
                trace_id=active_trace_id,
                success=success,
                inventory_p95_ms=inventory_p95,
                order_p95_ms=order_p95,
                reason=reason,
                incident=final_incident,
            )
