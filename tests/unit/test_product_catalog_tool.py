from order_agent_ops.business.inventory_adapter import InventoryAdapter
from order_agent_ops.tools.product_catalog import ProductCatalogTool


def test_product_catalog_tool_only_returns_versioned_catalog_facts() -> None:
    adapter = InventoryAdapter()
    tool = ProductCatalogTool(adapter)

    snapshot = tool.read_catalog()

    assert snapshot.data_version == adapter.data_version
    assert snapshot.products == adapter.list_products()
    assert len(snapshot.products) == 43
    assert tool.get_product("SKU-039").name == "Royal Kludge RK84"
    assert tool.get_product("SKU-NOT-FOUND") is None
