import json
import socket
from decimal import Decimal
from pathlib import Path

from order_agent_ops.business.inventory_adapter import InventoryAdapter
from order_agent_ops.business.risk_profile_adapter import RiskProfileAdapter
from order_agent_ops.config import PROJECT_ROOT
from order_agent_ops.storage.database import Database
from order_agent_ops.storage.repositories import InventoryRepository
from scripts.seed_demo_data import seed_demo_data


def test_inventory_adapter_returns_explicit_found_and_missing_results(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    adapter = InventoryAdapter()

    in_stock = adapter.query("SKU-001")
    out_of_stock = adapter.query("SKU-002")
    missing = adapter.query("SKU-404")

    assert adapter.data_version == "2026-09-28.2"
    assert len(adapter.list_products()) >= 2
    assert in_stock.found is True
    assert in_stock.product is not None
    assert in_stock.product.unit_price == Decimal("399.00")
    assert in_stock.product.available_quantity == 12
    assert out_of_stock.found is True
    assert out_of_stock.product is not None
    assert out_of_stock.product.available_quantity == 0
    assert missing.found is False
    assert missing.product is None
    assert missing.reason == "SKU not found: SKU-404"
    assert not hasattr(adapter, "reserve")
    assert not hasattr(adapter, "deduct")


def test_inventory_catalog_includes_normalized_keyboard_examples() -> None:
    adapter = InventoryAdapter()

    assert len(adapter.list_products()) == 43

    mx_keys = adapter.query("SKU-016")
    pebble = adapter.query("SKU-017")
    rk84 = adapter.query("SKU-039")
    magic_keyboard = adapter.query("SKU-035")

    assert mx_keys.found is True
    assert mx_keys.product is not None
    assert mx_keys.product.name == "Logitech MX Keys S"
    assert mx_keys.product.category == "keyboard"
    assert mx_keys.product.unit_price == Decimal("899.00")
    assert mx_keys.product.attributes["source_category"] == "无线办公键盘"
    assert "programming" in mx_keys.product.attributes["suitable_for"]
    assert "backlit" in mx_keys.product.attributes["feature_tags"]

    assert pebble.found is True
    assert pebble.product is not None
    assert pebble.product.unit_price == Decimal("249.00")
    assert pebble.product.attributes["source_category"] == "便携无线键盘"

    assert rk84.found is True
    assert rk84.product is not None
    assert rk84.product.name == "Royal Kludge RK84"
    assert rk84.product.unit_price == Decimal("319.00")
    assert "wireless" in rk84.product.attributes["connection"]
    assert "dormitory" in rk84.product.attributes["suitable_for"]

    assert magic_keyboard.found is True
    assert magic_keyboard.product is not None
    assert magic_keyboard.product.name == "Apple Magic Keyboard with Touch ID"
    assert magic_keyboard.product.attributes["brand"] == "Apple"


def test_inventory_query_returns_a_copy_not_mutable_adapter_state() -> None:
    adapter = InventoryAdapter()
    first = adapter.query("SKU-001")
    assert first.product is not None
    first.product.available_quantity = 999

    second = adapter.query("SKU-001")

    assert second.product is not None
    assert second.product.available_quantity == 12


def test_risk_profile_adapter_returns_stable_facts_without_a_risk_decision(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    adapter = RiskProfileAdapter()

    known = adapter.query("USER-003")
    missing = adapter.query("USER-404")

    assert adapter.data_version == "2026-09-27"
    assert len(adapter.list_profiles()) == 3
    assert known.found is True
    assert known.profile is not None
    assert known.profile.account_age_days == 3
    assert known.profile.chargeback_count == 2
    assert known.profile.failed_payment_count_30d == 5
    assert known.profile.identity_verified is False
    assert "risk_level" not in type(known.profile).model_fields
    assert missing.found is False
    assert missing.profile is None


def test_default_adapters_do_not_use_network(tmp_path: Path, monkeypatch) -> None:
    def fail_network(*_args, **_kwargs):
        raise AssertionError("Business adapters must not access the network")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(socket, "create_connection", fail_network)

    assert InventoryAdapter().query("SKU-001").found is True
    assert RiskProfileAdapter().query("USER-001").found is True


def test_service_profile_and_deployment_contain_inventory_v20_baseline() -> None:
    profiles = json.loads(
        (PROJECT_ROOT / "data" / "service_profiles.json").read_text(encoding="utf-8")
    )["profiles"]
    deployments = json.loads(
        (PROJECT_ROOT / "data" / "deployments.json").read_text(encoding="utf-8")
    )["deployments"]

    profile = next(item for item in profiles if item["object_id"] == "inventory-agent")
    deployment = next(
        item for item in deployments if item["object_id"] == "inventory-agent"
    )

    assert profile["version"] == "v2.0"
    assert profile["dependencies"] == ["inventory-data-adapter"]
    assert profile["owner"] == "order-platform-team"
    assert profile["runbook"]
    assert deployment["version"] == "v2.0"
    assert deployment["status"] == "succeeded"


def test_seed_script_is_idempotent_and_does_not_duplicate_audit(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "demo.sqlite3"

    first = seed_demo_data(database_path)
    database = Database(database_path)
    inventory = InventoryRepository(database)
    first_records = inventory.list_all()
    with database.connection() as connection:
        first_audit_count = connection.execute(
            "SELECT COUNT(*) AS count FROM audit_events"
        ).fetchone()["count"]

    second = seed_demo_data(database_path)
    second_records = inventory.list_all()
    with database.connection() as connection:
        second_audit_count = connection.execute(
            "SELECT COUNT(*) AS count FROM audit_events"
        ).fetchone()["count"]

    assert first.products_processed == 43
    assert first.inserted == 43
    assert first.updated == 0
    assert second.products_processed == 43
    assert second.inserted == 0
    assert second.updated == 0
    assert second.unchanged == 43
    assert first_records == second_records
    assert len(second_records) == 43
    assert first_audit_count == second_audit_count == 43
