"""Evidence-bound operations diagnosis agent."""

from __future__ import annotations

import json
from typing import ClassVar

from pydantic import Field

from order_agent_ops.agents.base import BaseAgent
from order_agent_ops.domain.base import DomainModel, IncidentId, NonEmptyString
from order_agent_ops.domain.evidence import EvidenceRecord
from order_agent_ops.domain.incidents import DiagnosisResult, IncidentRecord
from order_agent_ops.models.provider import ModelTaskType
from order_agent_ops.tools.query_observability import ObservabilityDataType
from order_agent_ops.tools.query_service_context import ServiceContextInclude


class InvestigationPlan(DomainModel):
    incident_id: IncidentId
    object_id: NonEmptyString
    observability_data_types: list[ObservabilityDataType] = Field(min_length=1)
    service_context_includes: list[ServiceContextInclude] = Field(min_length=1)
    rationale: NonEmptyString


class OpsGuardianAgent(BaseAgent):
    """Plan evidence collection and infer only from supplied stored evidence."""

    name = "ops-guardian-agent"
    version = "v1.0"
    prompt_version = "ops-guardian-prompt-v2"
    output_schema: ClassVar[type[DiagnosisResult]] = DiagnosisResult

    def plan_investigation(
        self,
        incident: IncidentRecord,
        *,
        trace_id: str,
    ) -> InvestigationPlan:
        with self.trace_recorder.span(
            trace_id,
            self.name,
            "ops.plan_investigation",
        ):
            return InvestigationPlan(
                incident_id=incident.incident_id,
                object_id=incident.object_id,
                observability_data_types=[
                    ObservabilityDataType.METRICS,
                    ObservabilityDataType.LOGS,
                    ObservabilityDataType.TRACES,
                ],
                service_context_includes=[
                    ServiceContextInclude.DEPLOYMENTS,
                    ServiceContextInclude.CONFIGURATION,
                    ServiceContextInclude.DEPENDENCIES,
                    ServiceContextInclude.OWNER,
                    ServiceContextInclude.RUNBOOK,
                ],
                rationale=(
                    "Correlate runtime symptoms with deployment and service context "
                    f"for rule {incident.rule}"
                ),
            )

    def diagnose(
        self,
        incident: IncidentRecord,
        evidence: list[EvidenceRecord],
        *,
        trace_id: str,
    ) -> DiagnosisResult:
        with self.track_run(
            trace_id,
            action="ops.diagnose",
            model_task_type=ModelTaskType.OPS_GUARDIAN.value,
        ) as run_context:
            payload = {
                "incident": incident.model_dump(mode="json"),
                "evidence": [item.model_dump(mode="json") for item in evidence],
                "rules": {
                    "use_only_supplied_evidence_ids": True,
                    "separate_facts_from_inference": True,
                    "declare_missing_information_when_incomplete": True,
                },
            }
            return self.model_gateway.generate(
                [
                    {
                        "role": "system",
                        "content": (
                            "Diagnose the incident using only supplied evidence and return every "
                            "field in the exact supplied schema. Copy incident_id exactly. Each "
                            "fact and root-cause candidate must cite one or more evidence_ids that "
                            "occur verbatim in the supplied evidence. Separate observed facts from "
                            "inference, use confidence from 0 to 1, and list missing information "
                            "instead of overstating certainty. Every recommended action must cite "
                            "supplied evidence; high or critical actions require approval=true. "
                            "Valid shape: {\"incident_id\":\"INC-FROM-INPUT\",\"facts\":[{"
                            "\"statement\":\"Observed fact\",\"evidence_ids\":["
                            "\"E-FROM-INPUT\"]}],\"root_cause_candidates\":[{\"cause\":"
                            "\"Evidence-backed inference\",\"confidence\":0.8,"
                            "\"evidence_ids\":[\"E-FROM-INPUT\"]}],"
                            "\"missing_information\":[],\"recommended_actions\":[{\"action\":"
                            "\"rollback\",\"target\":\"object-from-input\",\"risk_level\":"
                            "\"high\",\"requires_approval\":true,\"evidence_ids\":["
                            "\"E-FROM-INPUT\"]}]}. Replace every placeholder with supplied values; "
                            "never output a placeholder or invent an evidence ID. Return JSON only."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            payload,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    },
                ],
                task_type=ModelTaskType.OPS_GUARDIAN.value,
                response_schema=DiagnosisResult,
                prompt_version=self.prompt_version,
                trace_id=trace_id,
                agent_run_id=run_context.run_id,
            )
