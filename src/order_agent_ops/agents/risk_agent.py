"""Risk agent that sends uncertainty to manual review."""

import json
from typing import ClassVar

from order_agent_ops.agents.base import BaseAgent
from order_agent_ops.business.risk_profile_adapter import RiskProfileAdapter
from order_agent_ops.domain.enums import RiskLevel
from order_agent_ops.domain.orders import RiskCheckResult
from order_agent_ops.models.provider import ModelTaskType


class RiskAgent(BaseAgent):
    name = "risk-agent"
    version = "v1.0"
    prompt_version = "risk-prompt-v2"
    output_schema: ClassVar[type[RiskCheckResult]] = RiskCheckResult

    def __init__(self, model_gateway, risk_profile_adapter: RiskProfileAdapter, **kwargs) -> None:
        super().__init__(model_gateway, **kwargs)
        self.risk_profile_adapter = risk_profile_adapter

    def run(self, *, user_id: str, trace_id: str) -> RiskCheckResult:
        try:
            with self.track_run(
                trace_id,
                action="risk.evaluate",
                model_task_type=ModelTaskType.RISK_AGENT.value,
            ) as run_context:
                lookup = self.risk_profile_adapter.query(user_id)
                if not lookup.found or lookup.profile is None:
                    return self._unknown_result(user_id, lookup.reason)

                profile = lookup.profile
                model_result = self.model_gateway.generate(
                    [
                        {
                            "role": "system",
                            "content": (
                                "Evaluate only the supplied user profile and return every field in "
                                "the exact supplied schema. Copy user_id exactly. risk_score must be "
                                "between 0 and 100; risk_level must be low, medium, high, or unknown. "
                                "An active, identity-verified account with zero chargebacks, low "
                                "recent failures, and ordinary activity is low risk and does not "
                                "require manual review. A restricted or unverified account, at "
                                "least two chargebacks, or highly abnormal activity is high risk "
                                "and requires manual review. evidence_ids must be [] because no "
                                "platform evidence IDs are supplied. Valid low-risk example: "
                                "{\"user_id\":\"USER-001\",\"risk_score\":10,"
                                "\"risk_level\":\"low\",\"reason\":\"Verified active account "
                                "with no chargebacks\",\"evidence_ids\":[],"
                                "\"requires_manual_review\":false}. Return JSON only."
                            ),
                        },
                        {
                            "role": "user",
                            "content": json.dumps(
                                profile.model_dump(mode="json"),
                                separators=(",", ":"),
                            ),
                        },
                    ],
                    task_type=ModelTaskType.RISK_AGENT.value,
                    response_schema=RiskCheckResult,
                    prompt_version=self.prompt_version,
                    trace_id=trace_id,
                    agent_run_id=run_context.run_id,
                )
                if (
                    profile.account_status != "active"
                    or not profile.identity_verified
                    or profile.chargeback_count >= 2
                ):
                    return RiskCheckResult(
                        user_id=profile.user_id,
                        risk_score=max(model_result.risk_score, 90),
                        risk_level=RiskLevel.HIGH,
                        reason="User facts triggered the deterministic high-risk guard",
                        evidence_ids=model_result.evidence_ids,
                        requires_manual_review=True,
                    )
                requires_manual_review = model_result.requires_manual_review or (
                    model_result.risk_level in {RiskLevel.HIGH, RiskLevel.UNKNOWN}
                )
                return RiskCheckResult(
                    user_id=profile.user_id,
                    risk_score=model_result.risk_score,
                    risk_level=model_result.risk_level,
                    reason=model_result.reason,
                    evidence_ids=model_result.evidence_ids,
                    requires_manual_review=requires_manual_review,
                )
        except Exception as error:
            return self._unknown_result(
                user_id,
                f"Risk evaluation unavailable ({type(error).__name__})",
            )

    @staticmethod
    def _unknown_result(user_id: str, reason: str) -> RiskCheckResult:
        return RiskCheckResult(
            user_id=user_id,
            risk_score=0,
            risk_level=RiskLevel.UNKNOWN,
            reason=reason,
            evidence_ids=[],
            requires_manual_review=True,
        )
