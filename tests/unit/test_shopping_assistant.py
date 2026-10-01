from decimal import Decimal
import json

import pytest

from order_agent_ops.agents.shopping_assistant import (
    InvalidCandidateReferenceError,
    ShoppingAssistantAgent,
)
from order_agent_ops.business.inventory_adapter import InventoryAdapter
from order_agent_ops.domain.shopping import (
    AgentCatalogSelection,
    AgentPurchaseIntentAnalysis,
    ParsedPurchaseIntent,
    ProductCandidate,
)
from order_agent_ops.models import MockModelProvider, MockTaskType, ModelGateway
from order_agent_ops.tools.product_catalog import ProductCatalogTool


class RecordingMockProvider(MockModelProvider):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.catalog_messages = None

    def generate(self, messages, *, task_type, response_schema=None):
        if task_type == MockTaskType.SHOPPING_CATALOG_SEARCH.value:
            self.catalog_messages = messages
        return super().generate(
            messages,
            task_type=task_type,
            response_schema=response_schema,
        )


class SequentialCatalogProvider(MockModelProvider):
    def __init__(self, catalog_responses: list[dict[str, object]]) -> None:
        super().__init__()
        self.catalog_responses = list(catalog_responses)
        self.catalog_requests = []

    def generate(self, messages, *, task_type, response_schema=None):
        if task_type != MockTaskType.SHOPPING_CATALOG_SEARCH.value:
            return super().generate(
                messages,
                task_type=task_type,
                response_schema=response_schema,
            )
        self.catalog_requests.append(messages)
        if not self.catalog_responses:
            raise AssertionError("Unexpected extra catalog-search model call")
        return json.dumps(
            self.catalog_responses.pop(0),
            ensure_ascii=False,
            separators=(",", ":"),
        )


def test_ollama_facing_shopping_schemas_require_every_output_field() -> None:
    assert set(AgentCatalogSelection.model_json_schema()["required"]) == {
        "selected_skus",
        "reason",
    }
    assert set(AgentPurchaseIntentAnalysis.model_json_schema()["required"]) == {
        "category",
        "quantity",
        "max_price",
        "use_case",
        "constraint_conflicts",
        "clarifying_question",
    }


def test_agent_infers_low_noise_from_shared_room_language() -> None:
    with ModelGateway(MockModelProvider()) as gateway:
        agent = ShoppingAssistantAgent(
            gateway,
            ProductCatalogTool(InventoryAdapter()),
        )
        messages = ["我想买一个500元以内、晚上在宿舍写代码、别影响室友的机械键盘"]
        result = agent.analyze(
            messages,
            trace_id="TRACE-SHOPPING-SEMANTIC",
        )
        catalog_result = agent.search_catalog(
            messages,
            result,
            trace_id="TRACE-SHOPPING-SEMANTIC",
        )

    assert result.ready_to_search is True
    assert result.category == "keyboard"
    assert result.quantity == 1
    assert result.max_price == Decimal("500")
    assert result.hard_constraints == []
    assert result.soft_preferences == []
    assert result.inferred_requirements == []
    assert catalog_result.candidates[0].attributes["noise_level"] in {"silent", "low"}
    records = agent.list_run_records("TRACE-SHOPPING-SEMANTIC")
    assert records[0].prompt_version == agent.analyze_prompt_version


def test_agent_requests_clarification_for_vague_requirement() -> None:
    with ModelGateway(MockModelProvider()) as gateway:
        result = ShoppingAssistantAgent(gateway).analyze(
            ["我想买一个好一点的键盘"],
            trace_id="TRACE-SHOPPING-VAGUE",
        )

    assert result.ready_to_search is False
    assert set(result.missing_information) == {"budget"}
    assert result.clarifying_question is not None


def test_agent_ignores_model_reported_preference_conflicts() -> None:
    with ModelGateway(MockModelProvider()) as gateway:
        result = ShoppingAssistantAgent(gateway).analyze(
            ["我想买一个预算100元以内、无线、全铝、旗舰机械键盘"],
            trace_id="TRACE-SHOPPING-CONFLICT",
        )

    assert result.ready_to_search is True
    assert result.constraint_conflicts == []
    assert result.clarifying_question is None
    assert result.max_price == Decimal("100")


def test_agent_rejects_recommendation_that_invents_a_sku() -> None:
    provider = MockModelProvider(
        responses={
            MockTaskType.SHOPPING_RANK: {
                "recommended_sku": "SKU-INVENTED",
                "alternative_skus": [],
                "reason": "Invented product",
                "tradeoffs": [],
            }
        }
    )
    candidate = ProductCandidate(
        sku="SKU-003",
        name="Silent Office Mechanical Keyboard",
        category="keyboard",
        unit_price=Decimal("329.00"),
        available_quantity=8,
        attributes={"noise_level": "silent"},
        matched_preferences=["quiet"],
        score=10,
    )
    with ModelGateway(provider) as gateway:
        agent = ShoppingAssistantAgent(gateway)
        intent = agent.analyze(
            ["我想买一个500元以内安静的机械键盘"],
            trace_id="TRACE-SHOPPING-HALLUCINATION",
        )
        with pytest.raises(InvalidCandidateReferenceError, match="SKU-INVENTED"):
            agent.rank_candidates(
                intent,
                [candidate],
                trace_id="TRACE-SHOPPING-HALLUCINATION",
            )


def test_agent_reads_catalog_tool_and_owns_candidate_matching() -> None:
    provider = RecordingMockProvider()
    with ModelGateway(provider) as gateway:
        agent = ShoppingAssistantAgent(
            gateway,
            ProductCatalogTool(InventoryAdapter()),
        )
        messages = ["我想买一个1000元以内、适合宿舍写代码的无线机械键盘"]
        intent = agent.analyze(messages, trace_id="TRACE-AGENT-CATALOG")
        result = agent.search_catalog(
            messages,
            intent,
            trace_id="TRACE-AGENT-CATALOG",
        )

    assert result.candidates
    assert result.recommendation is not None
    assert result.recommendation.recommended_sku == "SKU-009"
    assert result.recommendation.recommended_sku in {
        candidate.sku for candidate in result.candidates
    }
    assert MockTaskType.SHOPPING_CATALOG_SEARCH.value in provider.calls
    actions = {
        (span.component, span.action)
        for span in agent.trace_recorder.get_trace("TRACE-AGENT-CATALOG")
    }
    assert ("product-catalog-tool", "catalog.read") in actions
    assert ("shopping-assistant-agent", "shopping.catalog_search") in actions
    system_prompt = provider.catalog_messages[0]["content"]
    user_payload = json.loads(provider.catalog_messages[1]["content"])
    assert "unmet_preferences" not in system_prompt
    assert "tradeoffs" not in system_prompt
    assert "messages is the only user-authored source" in system_prompt
    assert "budget, wireless connection, mechanical keyboard type" in system_prompt
    assert "identify that specific compromise" in system_prompt
    assert agent.catalog_search_prompt_version == "shopping-catalog-search-prompt-v3"
    assert "parsed_intent" not in user_payload
    assert user_payload["safety_limits"] == {
        "category": "keyboard",
        "quantity": 1,
        "max_price": "1000",
    }
    assert user_payload["reason_requirements"] == [
        {"criterion": "budget", "description": "价格不超过1000元预算"},
        {"criterion": "wireless", "description": "支持无线连接"},
        {"criterion": "mechanical", "description": "采用机械键盘结构"},
        {"criterion": "programming", "description": "适合写代码或编程"},
    ]
    assert user_payload["catalog_fact_reading_rules"] == [
        "逐个SKU独立核对全部条件，不能拼接不同商品的属性",
        "无线要求connection包含wireless、bluetooth或2.4ghz",
        "机械要求keyboard_type为mechanical",
        "低噪音要求noise_level为low或silent；medium和high不满足",
        "编程适用要求suitable_for包含programming或描述明确支持编程",
        "如果上述特征有任何一项不满足，reason必须明确写出该项取舍",
    ]
    assert user_payload["catalog_product_columns"] == [
        "sku",
        "name",
        "unit_price",
        "keyboard_type",
        "noise_level",
        "connection",
        "suitable_for",
        "other_facts",
    ]
    assert all(
        len(product) == len(user_payload["catalog_product_columns"])
        and float(product[2]) <= 1000
        and str(product[0]).startswith("SKU-")
        for product in user_payload["catalog_products"]
    )


def test_catalog_selection_uses_original_words_not_structured_feature_fields() -> None:
    provider = MockModelProvider(
        responses={
            MockTaskType.SHOPPING_ANALYZE: {
                "category": "keyboard",
                "quantity": 1,
                "max_price": "1000",
                "use_case": "dormitory",
                "constraint_conflicts": [],
                "clarifying_question": None,
            }
        }
    )
    messages = ["我想买一个1000元以内、适合晚上在宿舍写代码的无线机械键盘"]
    with ModelGateway(provider) as gateway:
        agent = ShoppingAssistantAgent(
            gateway,
            ProductCatalogTool(InventoryAdapter()),
        )
        intent = agent.analyze(messages, trace_id="TRACE-ORIGINAL-WORDING")
        result = agent.search_catalog(
            messages,
            intent,
            trace_id="TRACE-ORIGINAL-WORDING",
        )

    assert result.recommendation is not None
    assert result.recommendation.recommended_sku == "SKU-009"
    recommended = result.candidates[0]
    assert recommended.attributes["keyboard_type"] == "mechanical"
    assert "wireless" in recommended.attributes["connection"]
    assert recommended.attributes["noise_level"] == "low"


def test_agent_discards_catalog_selection_that_invents_a_sku() -> None:
    provider = MockModelProvider(
        responses={
            MockTaskType.SHOPPING_CATALOG_SEARCH: {
                "selected_skus": ["SKU-INVENTED"],
                "reason": "该候选在预算内，采用安静的机械键盘结构。",
            }
        }
    )
    with ModelGateway(provider) as gateway:
        agent = ShoppingAssistantAgent(
            gateway,
            ProductCatalogTool(InventoryAdapter()),
        )
        messages = ["我想买一个500元以内安静的机械键盘"]
        intent = agent.analyze(messages, trace_id="TRACE-CATALOG-HALLUCINATION")
        result = agent.search_catalog(
            messages,
            intent,
            trace_id="TRACE-CATALOG-HALLUCINATION",
        )

    assert result.candidates == []
    assert result.recommendation is None
    assert result.no_match_reason == "该候选在预算内，采用安静的机械键盘结构。"


def test_agent_retries_reason_that_falsely_attributes_catalog_sku_to_user() -> None:
    provider = SequentialCatalogProvider(
        [
            {
                "selected_skus": ["SKU-037"],
                "reason": (
                    "用户未明确指定具体产品，但提供了SKU-037作为示例，"
                    "因此选择该SKU进行分析。"
                ),
            },
            {
                "selected_skus": ["SKU-009"],
                "reason": (
                    "SKU-009在1000元预算内，采用无线连接和机械结构，"
                    "低噪音特性也更适合夜间宿舍编程。"
                ),
            },
        ]
    )
    messages = ["我想买一个1000元以内、适合晚上在宿舍写代码的无线机械键盘"]
    with ModelGateway(provider) as gateway:
        agent = ShoppingAssistantAgent(
            gateway,
            ProductCatalogTool(InventoryAdapter()),
        )
        intent = agent.analyze(messages, trace_id="TRACE-REASON-CORRECTION")
        result = agent.search_catalog(
            messages,
            intent,
            trace_id="TRACE-REASON-CORRECTION",
        )

    assert result.recommendation is not None
    assert result.recommendation.recommended_sku == "SKU-009"
    assert "无线连接" in result.recommendation.reason
    assert len(provider.catalog_requests) == 2
    retry_payload = json.loads(provider.catalog_requests[1][-1]["content"])
    assert retry_payload["correction"] == {
        "error": "recommendation_reason_not_grounded",
        "invalid_skus": ["SKU-037"],
        "missing_reason_criteria": [
            "价格不超过1000元预算",
            "支持无线连接",
            "采用机械键盘结构",
            "适合夜间宿舍使用并优先低噪音",
            "适合写代码或编程",
        ],
        "unacknowledged_tradeoffs": [
            "SKU-037 connection=['wired']",
            "SKU-037 noise_level=medium",
        ],
        "instruction": (
            "请重新比较目录并改写输出。只能把 messages 当作用户原话；"
            "invalid_skus 并非用户提供。reason 必须逐项写出 "
            "missing_reason_criteria 中的每一项，结合所选商品的目录事实说明"
            "满足情况；并根据 unacknowledged_tradeoffs 中列出的真实属性"
            "纠正错误结论。若不满足，必须明确写出具体取舍。"
        ),
    }


def test_agent_retries_reason_that_omits_requested_criteria() -> None:
    provider = SequentialCatalogProvider(
        [
            {
                "selected_skus": ["SKU-009"],
                "reason": "SKU-009的价格在1000元预算内。",
            },
            {
                "selected_skus": ["SKU-009"],
                "reason": (
                    "SKU-009在1000元预算内，采用无线连接和机械结构，"
                    "低噪音特性适合夜间宿舍编程，没有需要说明的功能取舍。"
                ),
            },
        ]
    )
    messages = ["我想买一个1000元以内、适合晚上在宿舍写代码的无线机械键盘"]
    with ModelGateway(provider) as gateway:
        agent = ShoppingAssistantAgent(
            gateway,
            ProductCatalogTool(InventoryAdapter()),
        )
        intent = agent.analyze(messages, trace_id="TRACE-REASON-CHECKLIST")
        result = agent.search_catalog(
            messages,
            intent,
            trace_id="TRACE-REASON-CHECKLIST",
        )

    assert result.recommendation is not None
    assert result.recommendation.recommended_sku == "SKU-009"
    assert len(provider.catalog_requests) == 2
    retry_payload = json.loads(provider.catalog_requests[1][-1]["content"])
    assert retry_payload["correction"]["invalid_skus"] == []
    assert retry_payload["correction"]["unacknowledged_tradeoffs"] == []
    assert retry_payload["correction"]["missing_reason_criteria"] == [
        "支持无线连接",
        "采用机械键盘结构",
        "适合夜间宿舍使用并优先低噪音",
        "适合写代码或编程",
    ]


def test_agent_normalizes_unique_sku_suffix_and_retries_false_feature_claim() -> None:
    provider = SequentialCatalogProvider(
        [
            {
                "selected_skus": ["003", "009"],
                "reason": (
                    "SKU-003在预算内，支持无线机械结构、低噪音并适合夜间宿舍编程。"
                ),
            },
            {
                "selected_skus": ["SKU-009: Compact Dorm Keyboard"],
                "reason": (
                    "SKU-009在1000元预算内，采用无线连接和机械结构，"
                    "低噪音特性适合夜间宿舍编程。"
                ),
            },
        ]
    )
    messages = ["我想买一个1000元以内、适合晚上在宿舍写代码的无线机械键盘"]
    with ModelGateway(provider) as gateway:
        agent = ShoppingAssistantAgent(
            gateway,
            ProductCatalogTool(InventoryAdapter()),
        )
        intent = agent.analyze(messages, trace_id="TRACE-SKU-NORMALIZATION")
        result = agent.search_catalog(
            messages,
            intent,
            trace_id="TRACE-SKU-NORMALIZATION",
        )

    assert result.recommendation is not None
    assert result.recommendation.recommended_sku == "SKU-009"
    assert len(provider.catalog_requests) == 2
    retry_payload = json.loads(provider.catalog_requests[1][-1]["content"])
    assert retry_payload["correction"]["unacknowledged_tradeoffs"] == [
        "SKU-003 connection=['wired']"
    ]


def test_agent_uses_catalog_facts_when_retry_still_omits_tradeoff() -> None:
    invalid_reason = {
        "selected_skus": ["SKU-016"],
        "reason": (
            "SKU-016在1000元预算内，支持无线机械键盘，静音并适合夜间宿舍编程。"
        ),
    }
    provider = SequentialCatalogProvider([invalid_reason, invalid_reason])
    messages = ["我想买一个1000元以内、适合晚上在宿舍写代码的无线机械键盘"]
    with ModelGateway(provider) as gateway:
        agent = ShoppingAssistantAgent(
            gateway,
            ProductCatalogTool(InventoryAdapter()),
        )
        intent = agent.analyze(messages, trace_id="TRACE-REASON-FACT-FALLBACK")
        result = agent.search_catalog(
            messages,
            intent,
            trace_id="TRACE-REASON-FACT-FALLBACK",
        )

    assert result.recommendation is not None
    assert result.recommendation.recommended_sku == "SKU-016"
    assert "keyboard_type=membrane" in result.recommendation.reason
    assert "不满足机械键盘要求" in result.recommendation.reason
    assert "noise_level=silent" in result.recommendation.reason
    assert len(provider.catalog_requests) == 2


def test_agent_allows_user_sku_attribution_when_original_message_contains_it() -> None:
    provider = SequentialCatalogProvider(
        [
            {
                "selected_skus": ["SKU-009"],
                "reason": (
                    "用户明确指定了SKU-009；它在预算内，并满足无线、机械和低噪音需求，"
                    "适合夜间宿舍编程。"
                ),
            }
        ]
    )
    messages = ["我想买一个键盘，指定SKU-009，预算1000元，用于晚上在宿舍写代码"]
    with ModelGateway(provider) as gateway:
        agent = ShoppingAssistantAgent(
            gateway,
            ProductCatalogTool(InventoryAdapter()),
        )
        intent = agent.analyze(messages, trace_id="TRACE-USER-SKU")
        result = agent.search_catalog(
            messages,
            intent,
            trace_id="TRACE-USER-SKU",
        )

    assert result.recommendation is not None
    assert result.recommendation.recommended_sku == "SKU-009"
    assert len(provider.catalog_requests) == 1


def test_agent_rejects_false_user_sku_attribution_after_one_retry() -> None:
    invalid_selection = {
        "selected_skus": ["SKU-037"],
        "reason": "用户提供了SKU-037作为示例，因此选择该商品。",
    }
    provider = SequentialCatalogProvider([invalid_selection, invalid_selection])
    messages = ["我想买一个1000元以内的无线机械键盘"]
    with ModelGateway(provider) as gateway:
        agent = ShoppingAssistantAgent(
            gateway,
            ProductCatalogTool(InventoryAdapter()),
        )
        intent = agent.analyze(messages, trace_id="TRACE-REASON-REJECTED")
        with pytest.raises(
            InvalidCandidateReferenceError,
            match="false user SKU attribution: SKU-037",
        ):
            agent.search_catalog(
                messages,
                intent,
                trace_id="TRACE-REASON-REJECTED",
            )

    assert len(provider.catalog_requests) == 2

