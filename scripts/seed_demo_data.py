"""Seed deterministic product quantities into the local SQLite database."""

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from order_agent_ops.business.inventory_adapter import (
    DEFAULT_PRODUCTS_PATH,
    InventoryAdapter,
)
from order_agent_ops.config import PROJECT_ROOT
from order_agent_ops.storage.database import Database
from order_agent_ops.storage.repositories import InventoryRepository


DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "demo.sqlite3"


@dataclass(frozen=True, slots=True)
class SeedSummary:
    database_path: str
    products_processed: int
    inserted: int
    updated: int
    unchanged: int


def _project_path(path: Path) -> Path:
    return path if path.is_absolute() else PROJECT_ROOT / path


def seed_demo_data(
    database_path: Path = DEFAULT_DATABASE_PATH,
    products_path: Path = DEFAULT_PRODUCTS_PATH,
) -> SeedSummary:
    """Create or update only changed inventory rows, making repeated runs idempotent."""

    resolved_database_path = _project_path(Path(database_path))
    resolved_products_path = _project_path(Path(products_path))
    database = Database(resolved_database_path)
    database.initialize()
    inventory = InventoryRepository(database)
    products = InventoryAdapter(resolved_products_path).list_products()

    inserted = 0
    updated = 0
    unchanged = 0
    for product in products:
        existing = inventory.get(product.sku)
        if existing is None:
            inventory.set_quantity(product.sku, product.available_quantity)
            inserted += 1
        elif existing.quantity != product.available_quantity:
            inventory.set_quantity(product.sku, product.available_quantity)
            updated += 1
        else:
            unchanged += 1

    return SeedSummary(
        database_path=str(resolved_database_path),
        products_processed=len(products),
        inserted=inserted,
        updated=updated,
        unchanged=unchanged,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite path; relative paths are resolved from the project root.",
    )
    parser.add_argument(
        "--products",
        type=Path,
        default=DEFAULT_PRODUCTS_PATH,
        help="Product JSON path; relative paths are resolved from the project root.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    summary = seed_demo_data(arguments.database, arguments.products)
    print(json.dumps(asdict(summary), ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
