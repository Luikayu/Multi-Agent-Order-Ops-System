"""Deterministic product filtering and preference scoring."""

from typing import Any

from order_agent_ops.business.inventory_adapter import InventoryAdapter, ProductSnapshot
from order_agent_ops.domain.shopping import (
    AttributeRequirement,
    ParsedPurchaseIntent,
    ProductCandidate,
)


class ProductSearchAdapter:
    """Search only trusted local product facts; this component never calls a model."""

    def __init__(self, inventory_adapter: InventoryAdapter) -> None:
        self.inventory_adapter = inventory_adapter

    def get_product(self, sku: str) -> ProductSnapshot | None:
        lookup = self.inventory_adapter.query(sku)
        return lookup.product if lookup.found else None

    def search(
        self,
        intent: ParsedPurchaseIntent,
        *,
        limit: int = 5,
    ) -> list[ProductCandidate]:
        if not intent.ready_to_search:
            raise ValueError("Product search requires a ready purchase intent")
        if limit <= 0:
            raise ValueError("Product search limit must be positive")

        candidates: list[ProductCandidate] = []
        for product in self.inventory_adapter.list_products():
            if not self._passes_hard_filters(product, intent):
                continue

            matched: list[str] = []
            unmet: list[str] = []
            for preference in intent.soft_preferences:
                label = f"{preference.attribute}: {preference.reason}"
                if self._matches(product, preference):
                    matched.append(label)
                else:
                    unmet.append(label)

            score = len(matched) * 10
            if intent.use_case and self._attribute_contains(
                product.attributes.get("suitable_for"), intent.use_case
            ):
                score += 5
                matched.append(f"use_case: suitable for {intent.use_case}")

            candidates.append(
                ProductCandidate(
                    sku=product.sku,
                    name=product.name,
                    category=product.category,
                    unit_price=product.unit_price,
                    available_quantity=product.available_quantity,
                    attributes=product.attributes,
                    matched_preferences=matched,
                    unmet_preferences=unmet,
                    score=score,
                )
            )

        candidates.sort(
            key=lambda candidate: (
                -candidate.score,
                candidate.unit_price,
                candidate.sku,
            )
        )
        return candidates[:limit]

    def _passes_hard_filters(
        self, product: ProductSnapshot, intent: ParsedPurchaseIntent
    ) -> bool:
        if not product.active or product.available_quantity < (intent.quantity or 1):
            return False
        if intent.category and product.category.casefold() != intent.category.casefold():
            return False
        if intent.max_price is not None and product.unit_price > intent.max_price:
            return False
        return all(self._matches(product, item) for item in intent.hard_constraints)

    @classmethod
    def _matches(
        cls, product: ProductSnapshot, requirement: AttributeRequirement
    ) -> bool:
        actual = product.attributes.get(requirement.attribute)
        return any(
            cls._attribute_contains(actual, expected)
            for expected in requirement.acceptable_values
        )

    @staticmethod
    def _attribute_contains(actual: Any, expected: str) -> bool:
        normalized_expected = expected.strip().casefold()
        if isinstance(actual, list):
            return any(str(value).strip().casefold() == normalized_expected for value in actual)
        return str(actual).strip().casefold() == normalized_expected
