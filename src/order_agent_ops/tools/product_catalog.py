"""Read-only business tool that exposes trusted local product facts to an Agent."""

from pydantic import Field

from order_agent_ops.business.inventory_adapter import (
    InventoryAdapter,
    ProductSnapshot,
)
from order_agent_ops.domain.base import DomainModel, NonEmptyString


class ProductCatalogSnapshot(DomainModel):
    """Versioned immutable catalog snapshot returned by the read-only tool."""

    data_version: NonEmptyString
    products: list[ProductSnapshot] = Field(default_factory=list)


class ProductCatalogTool:
    """Give the shopping Agent catalog facts without performing product matching."""

    name = "product-catalog-tool"
    version = "v1.0"

    def __init__(self, inventory_adapter: InventoryAdapter) -> None:
        self._inventory_adapter = inventory_adapter

    def read_catalog(self) -> ProductCatalogSnapshot:
        return ProductCatalogSnapshot(
            data_version=self._inventory_adapter.data_version,
            products=self._inventory_adapter.list_products(),
        )

    def get_product(self, sku: str) -> ProductSnapshot | None:
        lookup = self._inventory_adapter.query(sku)
        return lookup.product if lookup.found else None
