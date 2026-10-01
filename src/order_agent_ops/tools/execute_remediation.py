"""Formal approval-gated remediation tool."""

from order_agent_ops.domain.approvals import RemediationAction
from order_agent_ops.ops.remediation_executor import (
    ExecuteRemediationRequest,
    RemediationExecutor,
)


class ExecuteRemediationTool:
    name = "execute_remediation"
    version = "v1"

    def __init__(self, executor: RemediationExecutor) -> None:
        self._executor = executor

    def execute(self, request: ExecuteRemediationRequest) -> RemediationAction:
        return self._executor.execute(request)
