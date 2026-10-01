"""Rule-based anomaly detection and incident de-duplication."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from threading import RLock
from uuid import uuid4

from order_agent_ops.domain.agents import AgentRunRecord, ToolCallRecord
from order_agent_ops.domain.enums import IncidentStatus
from order_agent_ops.domain.incidents import IncidentRecord
from order_agent_ops.domain.orders import OrderRecord
from order_agent_ops.models.gateway import ModelCallRecord
from order_agent_ops.ops.rules import (
    AnomalyThresholds,
    DangerousActionObservation,
    HandoffObservation,
    RuleViolation,
    dangerous_action_violations,
    handoff_violations,
    latency_violations,
    model_call_violations,
    pending_order_violations,
    run_record_violations,
)
from order_agent_ops.storage.repositories import IncidentRepository
from order_agent_ops.telemetry.metrics import MetricRegistry


RecordProvider = Callable[[], Sequence[AgentRunRecord]]
ToolCallProvider = Callable[[], Sequence[ToolCallRecord]]
OrderProvider = Callable[[], Sequence[OrderRecord]]
ModelCallProvider = Callable[[], Sequence[ModelCallRecord]]


class AnomalyDetector:
    """Evaluate deterministic rules and merge duplicate active incidents."""

    _INACTIVE_STATUSES = {IncidentStatus.RESOLVED, IncidentStatus.CLOSED}

    def __init__(
        self,
        metrics: MetricRegistry,
        incidents: IncidentRepository,
        *,
        order_provider: OrderProvider | None = None,
        agent_run_providers: Sequence[RecordProvider] = (),
        tool_call_providers: Sequence[ToolCallProvider] = (),
        model_call_providers: Sequence[ModelCallProvider] = (),
        thresholds: AnomalyThresholds | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._metrics = metrics
        self._incidents = incidents
        self._order_provider = order_provider
        self._agent_run_providers = tuple(agent_run_providers)
        self._tool_call_providers = tuple(tool_call_providers)
        self._model_call_providers = tuple(model_call_providers)
        self.thresholds = thresholds or AnomalyThresholds()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = RLock()

    def detect(
        self,
        *,
        agent_runs: Sequence[AgentRunRecord] = (),
        tool_calls: Sequence[ToolCallRecord] = (),
        model_calls: Sequence[ModelCallRecord] = (),
        handoffs: Sequence[HandoffObservation] = (),
        dangerous_action_attempts: Sequence[DangerousActionObservation] = (),
    ) -> list[IncidentRecord]:
        """Run all initial rules and return incidents triggered in this pass."""

        now = self._clock()
        all_agent_runs = list(agent_runs)
        for provider in self._agent_run_providers:
            all_agent_runs.extend(provider())
        all_tool_calls = list(tool_calls)
        for provider in self._tool_call_providers:
            all_tool_calls.extend(provider())
        all_model_calls = list(model_calls)
        for provider in self._model_call_providers:
            all_model_calls.extend(provider())
        orders = list(self._order_provider()) if self._order_provider else []

        violations: list[RuleViolation] = []
        violations.extend(
            latency_violations(
                self._metrics.snapshot(), self.thresholds, occurred_at=now
            )
        )
        violations.extend(
            pending_order_violations(orders, self.thresholds, occurred_at=now)
        )
        violations.extend(
            run_record_violations(
                all_agent_runs,
                all_tool_calls,
                self.thresholds,
                occurred_at=now,
            )
        )
        violations.extend(
            model_call_violations(
                all_model_calls,
                self.thresholds,
                occurred_at=now,
            )
        )
        violations.extend(handoff_violations(handoffs))
        violations.extend(dangerous_action_violations(dangerous_action_attempts))

        with self._lock:
            return [
                self._upsert_incident(violation)
                for violation in self._coalesce_violations(violations)
            ]

    @staticmethod
    def _coalesce_violations(
        violations: Sequence[RuleViolation],
    ) -> list[RuleViolation]:
        combined: dict[tuple[str, str], RuleViolation] = {}
        for violation in violations:
            key = (violation.object_id, violation.rule)
            existing = combined.get(key)
            if existing is None:
                combined[key] = violation
                continue
            combined[key] = existing.model_copy(
                update={
                    "source_refs": list(
                        dict.fromkeys(
                            [*existing.source_refs, *violation.source_refs]
                        )
                    )
                }
            )
        return list(combined.values())

    def _upsert_incident(self, violation: RuleViolation) -> IncidentRecord:
        existing = next(
            (
                incident
                for incident in self._incidents.list_all()
                if incident.object_id == violation.object_id
                and incident.rule == violation.rule
                and incident.status not in self._INACTIVE_STATUSES
            ),
            None,
        )
        if existing is None:
            return self._incidents.create(
                IncidentRecord(
                    incident_id=f"INC-{uuid4().hex}",
                    status=IncidentStatus.OPEN,
                    object_id=violation.object_id,
                    rule=violation.rule,
                    summary=violation.summary,
                    occurred_at=violation.occurred_at,
                    source_refs=violation.source_refs,
                )
            )

        merged_refs = list(
            dict.fromkeys([*existing.source_refs, *violation.source_refs])
        )
        if merged_refs == existing.source_refs and existing.summary == violation.summary:
            return existing
        return self._incidents.update(
            existing.model_copy(
                update={
                    "summary": violation.summary,
                    "source_refs": merged_refs,
                }
            )
        )
