"""Read-only access to deterministic product and inventory facts."""

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import Field, model_validator

from order_agent_ops.config import PROJECT_ROOT
from order_agent_ops.domain.base import DomainModel, NonEmptyString


DEFAULT_PRODUCTS_PATH = PROJECT_ROOT / "data" / "products.json"


class ProductSnapshot(DomainModel):
    sku: NonEmptyString
    name: NonEmptyString
    category: NonEmptyString
    description: NonEmptyString
    unit_price: Decimal = Field(ge=0)
    available_quantity: int = Field(ge=0)
    active: bool
    attributes: dict[str, Any] = Field(default_factory=dict)


class InventoryLookupResult(DomainModel):
    found: bool
    product: ProductSnapshot | None
    reason: NonEmptyString

    @model_validator(mode="after")
    def found_matches_product(self) -> "InventoryLookupResult":
        if self.found != (self.product is not None):
            raise ValueError("Inventory lookup found flag must match product presence")
        return self


class InventoryAdapter:
    """Load an immutable snapshot and expose queries without mutation methods."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_PRODUCTS_PATH
        payload = self._read_payload(self.path)
        products = payload.get("products")
        if not isinstance(products, list):
            raise ValueError("Products data must contain a 'products' list")

        self.data_version = self._required_string(payload, "data_version")
        self._products: dict[str, ProductSnapshot] = {}
        for raw_product in products:
            product = ProductSnapshot.model_validate(raw_product)
            if product.sku in self._products:
                raise ValueError(f"Duplicate SKU in products data: {product.sku}")
            self._products[product.sku] = product

    @staticmethod
    def _read_payload(path: Path) -> dict[str, Any]:
        with path.open("r", encoding="utf-8") as data_file:
            payload = json.load(data_file)
        if not isinstance(payload, dict):
            raise ValueError(f"Products data must contain a JSON object: {path}")
        return payload

    @staticmethod
    def _required_string(payload: dict[str, Any], field: str) -> str:
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Products data requires non-empty '{field}'")
        return value.strip()

    def query(self, sku: str) -> InventoryLookupResult:
        normalized_sku = sku.strip()
        if not normalized_sku:
            raise ValueError("SKU must not be empty")
        product = self._products.get(normalized_sku)
        if product is None:
            return InventoryLookupResult(
                found=False,
                product=None,
                reason=f"SKU not found: {normalized_sku}",
            )
        return InventoryLookupResult(
            found=True,
            product=product.model_copy(deep=True),
            reason="Product snapshot found",
        )

    def list_products(self) -> list[ProductSnapshot]:
        return [product.model_copy(deep=True) for product in self._products.values()]
