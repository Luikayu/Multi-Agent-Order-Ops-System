"""Inventory agent with fail-closed query behavior."""

import json
from typing import ClassVar

from order_agent_ops.agents.base import BaseAgent
from order_agent_ops.business.inventory_adapter import InventoryAdapter
from order_agent_ops.domain.enums import InventoryStatus
from order_agent_ops.domain.orders import InventoryCheckResult
from order_agent_ops.models.errors import ModelTimeoutError
from order_agent_ops.models.provider import ModelTaskType


class InventoryAgent(BaseAgent):
    name = "inventory-agent"
    version = "v2.0"
    prompt_version = "inventory-prompt-v2"
    output_schema: ClassVar[type[InventoryCheckResult]] = InventoryCheckResult

    def __init__(self, model_gateway, inventory_adapter: InventoryAdapter, **kwargs) -> None:
        super().__init__(model_gateway, **kwargs)
        self.inventory_adapter = inventory_adapter

    def run(
        self,
        *,
        sku: str,
        requested_quantity: int,
        trace_id: str,
    ) -> InventoryCheckResult:
        if requested_quantity <= 0:
            raise ValueError("Requested inventory quantity must be greater than zero")

        try:
            with self.track_run(
                trace_id,
                action="inventory.check",
                model_task_type=ModelTaskType.INVENTORY_AGENT.value,
            ) as run_context:
                lookup = self.inventory_adapter.query(sku)
                if not lookup.found or lookup.product is None:
                    return InventoryCheckResult(
                        sku=sku,
                        requested_quantity=requested_quantity,
                        status=InventoryStatus.UNKNOWN,
                        available_quantity=None,
                        reason=lookup.reason,
                        evidence_ids=[],
                    )

                product = lookup.product
                model_result = self.model_gateway.generate(
                    [
                        {
                            "role": "system",
                            "content": (
                                "Evaluate only the supplied inventory facts and return every field "
                                "in the exact supplied schema. Copy sku, requested_quantity, and "
                                "available_quantity exactly. status must be sufficient when active "
                                "is true and available_quantity >= requested_quantity; insufficient "
                                "when active is true but quantity is lower; otherwise unknown. "
                                "evidence_ids must be [] because no platform evidence IDs are "
                                "provided. Valid example: {\"sku\":\"SKU-001\","
                                "\"requested_quantity\":1,\"status\":\"sufficient\","
                                "\"available_quantity\":12,\"reason\":\"Available quantity "
                                "covers the request\",\"evidence_ids\":[]}. Return JSON only."
                            ),
                        },
                        {
                            "role": "user",
                            "content": json.dumps(
                                {
                                    "sku": product.sku,
                                    "requested_quantity": requested_quantity,
                                    "available_quantity": product.available_quantity,
                                    "active": product.active,
                                },
                                separators=(",", ":"),
                            ),
                        },
                    ],
                    task_type=ModelTaskType.INVENTORY_AGENT.value,
                    response_schema=InventoryCheckResult,
                    prompt_version=self.prompt_version,
                    trace_id=trace_id,
                    agent_run_id=run_context.run_id,
                )

                status = model_result.status
                reason = model_result.reason
                if not product.active:
                    status = InventoryStatus.UNKNOWN
                    reason = "Product is inactive; inventory cannot be approved"
                elif product.available_quantity < requested_quantity:
                    status = InventoryStatus.INSUFFICIENT
                    reason = "Available inventory is below requested quantity"

                return InventoryCheckResult(
                    sku=product.sku,
                    requested_quantity=requested_quantity,
                    status=status,
                    available_quantity=product.available_quantity,
                    reason=reason,
                    evidence_ids=model_result.evidence_ids,
                )
        except (ModelTimeoutError, TimeoutError) as error:
            return InventoryCheckResult(
                sku=sku,
                requested_quantity=requested_quantity,
                status=InventoryStatus.TIMEOUT,
                available_quantity=None,
                reason=f"Inventory check timed out ({type(error).__name__})",
                evidence_ids=[],
            )
        except Exception as error:
            return InventoryCheckResult(
                sku=sku,
                requested_quantity=requested_quantity,
                status=InventoryStatus.UNKNOWN,
                available_quantity=None,
                reason=f"Inventory check unavailable ({type(error).__name__})",
                evidence_ids=[],
            )
