from decimal import Decimal

from order_agent_ops.business.inventory_adapter import InventoryAdapter
from order_agent_ops.business.product_search_adapter import ProductSearchAdapter
from order_agent_ops.domain.shopping import AttributeRequirement, ParsedPurchaseIntent


def quiet_keyboard_intent(*, max_price: str = "500") -> ParsedPurchaseIntent:
    return ParsedPurchaseIntent(
        category="keyboard",
        quantity=1,
        max_price=Decimal(max_price),
        use_case="dormitory",
        hard_constraints=[
            AttributeRequirement(
                attribute="keyboard_type",
                acceptable_values=["mechanical"],
                reason="Mechanical keyboard requested",
            )
        ],
        soft_preferences=[
            AttributeRequirement(
                attribute="noise_level",
                acceptable_values=["silent", "low"],
                reason="Shared room requires low noise",
            )
        ],
        inferred_requirements=[],
        missing_information=[],
        constraint_conflicts=[],
        clarifying_question=None,
        ready_to_search=True,
    )


def test_search_uses_hard_filters_and_stable_preference_scoring() -> None:
    adapter = ProductSearchAdapter(InventoryAdapter())

    first = adapter.search(quiet_keyboard_intent())
    second = adapter.search(quiet_keyboard_intent())

    assert first == second
    assert first
    assert all(item.category == "keyboard" for item in first)
    assert all(item.unit_price <= Decimal("500") for item in first)
    assert all(item.attributes["keyboard_type"] == "mechanical" for item in first)
    assert all(item.sku != "SKU-011" for item in first)
    assert any(item.attributes["noise_level"] == "silent" for item in first)


def test_search_does_not_relax_hard_constraints_when_no_product_matches() -> None:
    intent = quiet_keyboard_intent(max_price="100")

    assert ProductSearchAdapter(InventoryAdapter()).search(intent) == []


def test_search_can_filter_new_product_specific_feature_tags() -> None:
    intent = ParsedPurchaseIntent(
        category="keyboard",
        quantity=1,
        max_price=Decimal("300"),
        use_case="travel",
        hard_constraints=[
            AttributeRequirement(
                attribute="feature_tags",
                acceptable_values=["portable"],
                reason="A portable keyboard is required",
            )
        ],
        soft_preferences=[],
        inferred_requirements=[],
        missing_information=[],
        constraint_conflicts=[],
        clarifying_question=None,
        ready_to_search=True,
    )

    candidates = ProductSearchAdapter(InventoryAdapter()).search(intent)

    assert [candidate.sku for candidate in candidates] == ["SKU-017"]
    assert candidates[0].name == "Logitech Pebble Keys 2 K380s"
    assert "use_case: suitable for travel" in candidates[0].matched_preferences


def test_search_finds_budget_wireless_mechanical_keyboards() -> None:
    intent = ParsedPurchaseIntent(
        category="keyboard",
        quantity=1,
        max_price=Decimal("500"),
        use_case="dormitory",
        hard_constraints=[
            AttributeRequirement(
                attribute="keyboard_type",
                acceptable_values=["mechanical"],
                reason="A mechanical keyboard is required",
            ),
            AttributeRequirement(
                attribute="connection",
                acceptable_values=["wireless"],
                reason="A wireless connection is required",
            ),
        ],
        soft_preferences=[],
        inferred_requirements=[],
        missing_information=[],
        constraint_conflicts=[],
        clarifying_question=None,
        ready_to_search=True,
    )

    candidates = ProductSearchAdapter(InventoryAdapter()).search(intent)

    assert candidates
    assert {"SKU-009", "SKU-038", "SKU-039"}.issubset(
        {candidate.sku for candidate in candidates}
    )
    assert all(candidate.unit_price <= Decimal("500") for candidate in candidates)
    assert all(candidate.attributes["keyboard_type"] == "mechanical" for candidate in candidates)
    assert all("wireless" in candidate.attributes["connection"] for candidate in candidates)

