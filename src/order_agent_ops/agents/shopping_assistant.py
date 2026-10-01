"""Natural-language purchase analysis and grounded candidate comparison."""

import json
import re
from typing import ClassVar

from order_agent_ops.agents.base import BaseAgent
from order_agent_ops.business.inventory_adapter import ProductSnapshot
from order_agent_ops.domain.shopping import (
    AgentCatalogSelection,
    AgentPurchaseIntentAnalysis,
    GroundedCatalogSearchResult,
    ParsedPurchaseIntent,
    ProductCandidate,
    ProductRecommendation,
)
from order_agent_ops.models.gateway import ModelGateway
from order_agent_ops.models.provider import ModelTaskType
from order_agent_ops.telemetry.tracing import TraceRecorder
from order_agent_ops.tools.product_catalog import ProductCatalogTool


class InvalidCandidateReferenceError(ValueError):
    """Raised when a model recommendation references an untrusted SKU."""


class ShoppingAssistantAgent(BaseAgent):
    name = "shopping-assistant-agent"
    version = "v2.4"
    prompt_version = "shopping-analyze-prompt-v4"
    analyze_prompt_version = "shopping-analyze-prompt-v4"
    rank_prompt_version = "shopping-rank-prompt-v2"
    catalog_search_prompt_version = "shopping-catalog-search-prompt-v3"
    output_schema: ClassVar[type[ParsedPurchaseIntent]] = ParsedPurchaseIntent

    _sku_pattern = re.compile(
        r"(?<![A-Za-z0-9._-])SKU-[A-Za-z0-9][A-Za-z0-9._-]*"
        r"(?![A-Za-z0-9._-])",
        re.IGNORECASE,
    )
    _chinese_user_claim_verbs = re.compile(
        r"提供(?:了)?|指定(?:了)?|给出(?:了)?|输入(?:了)?|提到(?:了)?|"
        r"要求(?:了)?|选择(?:了)?"
    )
    _english_user_claim_verbs = re.compile(
        r"\b(?:provided|specified|gave|entered|mentioned|requested|selected)\b",
        re.IGNORECASE,
    )
    _reason_criterion_keywords = {
        "budget": ("预算", "价格", "价位", "元", "budget", "price", "cost"),
        "wireless": (
            "无线",
            "蓝牙",
            "2.4ghz",
            "wireless",
            "bluetooth",
        ),
        "mechanical": ("机械", "机械轴", "mechanical"),
        "nighttime_low_noise": (
            "低噪",
            "静音",
            "安静",
            "噪音",
            "声音",
            "夜间",
            "晚上",
            "室友",
            "low-noise",
            "quiet",
            "silent",
            "noise",
        ),
        "programming": ("写代码", "编程", "程序开发", "programming", "coding"),
    }
    _model_catalog_columns = (
        "sku",
        "name",
        "unit_price",
        "keyboard_type",
        "noise_level",
        "connection",
        "suitable_for",
        "other_facts",
    )

    _catalog_search_system_prompt = (
        "重要规则：只有 messages 字段是用户原话，目录里的 SKU 不是用户提供的。"
        "先比较并优先选择同时满足 reason_requirements 的商品；如果只能近似满足，"
        "仍可推荐，但 reason 必须明确写出不满足项和取舍。reason 必须逐项覆盖 "
        "reason_requirements 中的每个条件，例如预算、无线、机械、夜间低噪音和编程，"
        "不得只写笼统结论。必须对每个 SKU 分别核对，不能把不同商品的优点拼成一个"
        "商品的结论；noise_level=medium/high 不等于低噪音。"
        "Choose the products that best satisfy the user's original wording. "
        "The JSON field messages is the only user-authored source. safety_limits, "
        "catalog_data_version, and catalog_products are system-provided context. Never "
        "claim that the user provided, specified, mentioned, selected, or used a SKU as "
        "an example unless that exact SKU occurs verbatim in messages. "
        "The supplied catalog contains only real products that already pass category, "
        "budget, active-status, and stock safety checks. Compare descriptions and all "
        "heterogeneous attributes semantically. reason_requirements is a system-generated "
        "explanation checklist derived from messages; use every listed item in both ranking "
        "and reason, but do not treat it as an additional user message or a rigid filter. "
        "Evaluate the top selection against the budget and every explicit or implied feature "
        "in messages. For example, a request "
        "for a wireless mechanical keyboard for nighttime dormitory programming requires "
        "the reason to discuss the budget, wireless connection, mechanical keyboard type, "
        "and low-noise suitability. Prefer products satisfying those needs, but a close "
        "alternative is allowed when no product is exact. If a selected product does not "
        "meet a requested feature, identify that specific compromise in reason. Do not turn "
        "feature preferences into rigid equality rules, and do not use SKU sequence or "
        "catalog position as a ranking signal. Return exactly two fields: selected_skus and "
        "reason. selected_skus must contain one to five exact supplied catalog SKUs in "
        "best-first order. reason must be one concise, evidence-grounded overall explanation "
        "covering the requested criteria and any compromise. If no supplied product satisfies "
        "the feature needs even approximately, return selected_skus=[] and explain why in "
        "reason. A valid response shape is {\"selected_skus\":[\"SKU-A\",\"SKU-B\"],"
        "\"reason\":\"Both are within budget; SKU-A best matches the requested features, "
        "while SKU-B has a stated compromise\"}. Replace example values with supplied catalog "
        "facts. Never invent or modify a SKU, name, attribute, price, or inventory value. "
        "Return JSON only in the exact supplied schema."
    )

    def __init__(
        self,
        model_gateway: ModelGateway,
        catalog_tool: ProductCatalogTool | None = None,
        *,
        trace_recorder: TraceRecorder | None = None,
    ) -> None:
        super().__init__(model_gateway, trace_recorder=trace_recorder)
        self.catalog_tool = catalog_tool

    def analyze(
        self,
        messages: list[str],
        *,
        trace_id: str,
    ) -> ParsedPurchaseIntent:
        if not messages or any(not message.strip() for message in messages):
            raise ValueError("Shopping analysis requires non-empty user messages")
        with self.track_run(
            trace_id,
            action="shopping.analyze",
            model_task_type=ModelTaskType.SHOPPING_ANALYZE.value,
            prompt_version=self.analyze_prompt_version,
        ) as run_context:
            analysis = self.model_gateway.generate(
                [
                    {
                        "role": "system",
                        "content": (
                            "Extract only the basic catalog-search envelope into the exact "
                            "supplied schema: category, quantity, max_price, use_case, "
                            "constraint_conflicts, and clarifying_question. Do not translate "
                            "product features such as mechanical, wireless, quiet, layout, "
                            "material, switch type, or battery life into fields; the product Agent "
                            "will reason over those words directly later. A brand, switch model, "
                            "battery specification, and layout are never required to start search. "
                            "Use category='keyboard' for a keyboard, quantity=1 for a singular "
                            "request, the stated numeric budget for max_price, and a short use-case "
                            "summary such as 'dormitory programming'. constraint_conflicts is retained "
                            "only for schema compatibility and must always be an empty list. Never "
                            "classify combinations of product preferences such as wireless, mechanical, "
                            "quiet, premium, or material as conflicts. If category, quantity, and budget "
                            "are present, clarifying_question must be null. Otherwise ask exactly one "
                            "useful question for the missing information. Return JSON only."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"messages": messages},
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    },
                ],
                task_type=ModelTaskType.SHOPPING_ANALYZE.value,
                response_schema=AgentPurchaseIntentAnalysis,
                prompt_version=self.analyze_prompt_version,
                trace_id=trace_id,
                agent_run_id=run_context.run_id,
            )

        missing_information: list[str] = []
        if analysis.category is None:
            missing_information.append("product_category")
        if analysis.quantity is None:
            missing_information.append("quantity")
        if analysis.max_price is None:
            missing_information.append("budget")
        # Conflict detection is intentionally disabled. Small local models can mistake
        # compatible product preferences (for example wireless + mechanical) for a
        # contradiction. Feature feasibility is handled as an explainable catalog
        # tradeoff instead of blocking search.
        ready_to_search = not missing_information
        clarifying_question = None
        if not ready_to_search:
            missing = "、".join(missing_information)
            clarifying_question = f"请补充以下信息：{missing}。"
        return ParsedPurchaseIntent(
            category=analysis.category,
            quantity=analysis.quantity,
            max_price=analysis.max_price,
            use_case=analysis.use_case,
            hard_constraints=[],
            soft_preferences=[],
            inferred_requirements=[],
            missing_information=missing_information,
            constraint_conflicts=[],
            clarifying_question=clarifying_question,
            ready_to_search=ready_to_search,
        )

    @staticmethod
    def _sentence_prefix(text: str, end: int, *, max_length: int = 160) -> str:
        prefix = text[max(0, end - max_length) : end]
        last_boundary = max(prefix.rfind(mark) for mark in "。！？.!?；;\n")
        return prefix[last_boundary + 1 :]

    @classmethod
    def _prefix_attributes_sku_to_user(cls, prefix: str) -> bool:
        chinese_user_at = max(prefix.rfind("用户"), prefix.rfind("客户"))
        if chinese_user_at >= 0:
            segment = prefix[chinese_user_at:]
            verbs = list(cls._chinese_user_claim_verbs.finditer(segment))
            if verbs:
                last_verb = verbs[-1]
                between_verb_and_sku = segment[last_verb.end() :]
                before_verb = segment[max(0, last_verb.start() - 8) : last_verb.start()]
                negated = re.search(
                    r"(?:未|没有|并未|从未|不曾|并没有)(?:明确)?\s*$",
                    before_verb,
                )
                if len(between_verb_and_sku) <= 24 and negated is None:
                    return True

        lowered = prefix.casefold()
        english_user_at = max(lowered.rfind("user"), lowered.rfind("customer"))
        if english_user_at >= 0:
            segment = prefix[english_user_at:]
            verbs = list(cls._english_user_claim_verbs.finditer(segment))
            if verbs:
                last_verb = verbs[-1]
                between_verb_and_sku = segment[last_verb.end() :]
                before_verb = segment[max(0, last_verb.start() - 20) : last_verb.start()]
                negated = re.search(
                    r"(?:not|never|did\s+not|has\s+not|had\s+not)\s*$",
                    before_verb,
                    re.IGNORECASE,
                )
                if len(between_verb_and_sku) <= 48 and negated is None:
                    return True
        return False

    @classmethod
    def _falsely_user_attributed_skus(
        cls,
        reason: str,
        messages: list[str],
    ) -> list[str]:
        original_text = "\n".join(messages).casefold()
        invalid: set[str] = set()
        for match in cls._sku_pattern.finditer(reason):
            sku = match.group(0)
            if sku.casefold() in original_text:
                continue
            prefix = cls._sentence_prefix(reason, match.start())
            if cls._prefix_attributes_sku_to_user(prefix):
                invalid.add(sku.upper())
        return sorted(invalid)

    @classmethod
    def _build_reason_requirements(
        cls,
        messages: list[str],
        intent: ParsedPurchaseIntent,
    ) -> list[dict[str, str]]:
        text = " ".join(messages)
        lowered = text.casefold()
        requirements: list[dict[str, str]] = []
        if intent.max_price is not None:
            requirements.append(
                {
                    "criterion": "budget",
                    "description": f"价格不超过{intent.max_price}元预算",
                }
            )
        if "无线" in text or "wireless" in lowered:
            requirements.append(
                {"criterion": "wireless", "description": "支持无线连接"}
            )
        if "机械" in text or "mechanical" in lowered:
            requirements.append(
                {"criterion": "mechanical", "description": "采用机械键盘结构"}
            )
        requests_quiet = any(
            token in text
            for token in ("不影响室友", "别影响室友", "安静", "静音", "低噪")
        ) or ("晚上" in text and "宿舍" in text)
        if requests_quiet or any(
            token in lowered for token in ("quiet", "silent", "low-noise")
        ):
            requirements.append(
                {
                    "criterion": "nighttime_low_noise",
                    "description": "适合夜间宿舍使用并优先低噪音",
                }
            )
        if (
            "写代码" in text
            or "编程" in text
            or "程序开发" in text
            or any(token in lowered for token in ("programming", "coding"))
        ):
            requirements.append(
                {"criterion": "programming", "description": "适合写代码或编程"}
            )
        return requirements

    @classmethod
    def _missing_reason_criteria(
        cls,
        reason: str,
        requirements: object,
    ) -> list[str]:
        if not isinstance(requirements, list):
            return []
        lowered = reason.casefold()
        missing: list[str] = []
        for requirement in requirements:
            if not isinstance(requirement, dict):
                continue
            criterion = requirement.get("criterion")
            description = requirement.get("description")
            if not isinstance(criterion, str) or not isinstance(description, str):
                continue
            keywords = cls._reason_criterion_keywords.get(criterion, ())
            if keywords and not any(keyword.casefold() in lowered for keyword in keywords):
                missing.append(description)
        return missing

    @staticmethod
    def _has_trusted_catalog_selection(
        selection: AgentCatalogSelection,
        payload: dict[str, object],
    ) -> bool:
        products = payload.get("catalog_products")
        if not isinstance(products, list):
            return False
        trusted_skus = {
            product.get("sku") if isinstance(product, dict) else product[0]
            for product in products
            if (
                isinstance(product, dict)
                and isinstance(product.get("sku"), str)
            )
            or (
                isinstance(product, list)
                and bool(product)
                and isinstance(product[0], str)
            )
        }
        return any(sku in trusted_skus for sku in selection.selected_skus)

    @classmethod
    def _trusted_model_products(
        cls,
        payload: dict[str, object],
    ) -> dict[str, dict[str, object]]:
        products = payload.get("catalog_products")
        columns = payload.get("catalog_product_columns")
        if not isinstance(products, list) or not isinstance(columns, list):
            return {}
        trusted: dict[str, dict[str, object]] = {}
        for raw_product in products:
            if isinstance(raw_product, dict):
                product = raw_product
            elif isinstance(raw_product, list):
                product = dict(zip(columns, raw_product, strict=False))
            else:
                continue
            sku = product.get("sku")
            if isinstance(sku, str):
                trusted[sku] = product
        return trusted

    @classmethod
    def _normalize_selected_skus(
        cls,
        selection: AgentCatalogSelection,
        payload: dict[str, object],
    ) -> AgentCatalogSelection:
        trusted = cls._trusted_model_products(payload)
        by_casefold = {sku.casefold(): sku for sku in trusted}
        normalized: list[str] = []
        for raw_sku in selection.selected_skus:
            candidate = raw_sku.strip()
            extracted = cls._sku_pattern.search(candidate)
            if extracted is not None:
                candidate = extracted.group(0)
            elif re.fullmatch(r"\d{3}", candidate):
                candidate = f"SKU-{candidate}"
            canonical = by_casefold.get(candidate.casefold(), raw_sku)
            if canonical not in normalized:
                normalized.append(canonical)
        return selection.model_copy(update={"selected_skus": normalized})

    @classmethod
    def _unacknowledged_primary_tradeoffs(
        cls,
        selection: AgentCatalogSelection,
        payload: dict[str, object],
    ) -> list[str]:
        if not selection.selected_skus:
            return []
        trusted = cls._trusted_model_products(payload)
        product = trusted.get(selection.selected_skus[0])
        requirements = payload.get("reason_requirements")
        if product is None or not isinstance(requirements, list):
            return []

        requested = {
            item.get("criterion")
            for item in requirements
            if isinstance(item, dict) and isinstance(item.get("criterion"), str)
        }
        reason = selection.reason.casefold()
        tradeoff_words = (
            "取舍",
            "折中",
            "不满足",
            "不支持",
            "未达到",
            "没有",
            "无法",
            "并非",
            "但",
            "不过",
            "tradeoff",
            "does not",
            "not support",
            "lacks",
        )

        checks: dict[str, tuple[bool, tuple[str, ...], str]] = {
            "wireless": (
                cls._attribute_contains_any(
                    product.get("connection"),
                    {"wireless", "bluetooth", "2.4ghz"},
                ),
                ("wired", "有线"),
                f"{selection.selected_skus[0]} connection={product.get('connection')}",
            ),
            "mechanical": (
                str(product.get("keyboard_type") or "").casefold() == "mechanical",
                ("membrane", "薄膜", "magnetic", "磁轴"),
                (
                    f"{selection.selected_skus[0]} "
                    f"keyboard_type={product.get('keyboard_type')}"
                ),
            ),
            "nighttime_low_noise": (
                str(product.get("noise_level") or "").casefold()
                in {"low", "silent"},
                ("medium", "high", "中等", "高噪"),
                (
                    f"{selection.selected_skus[0]} "
                    f"noise_level={product.get('noise_level')}"
                ),
            ),
            "programming": (
                cls._attribute_contains_any(
                    product.get("suitable_for"),
                    {"programming", "coding"},
                ),
                ("未明确", "未标注", "not listed", "not specified"),
                (
                    f"{selection.selected_skus[0]} "
                    f"suitable_for={product.get('suitable_for')}"
                ),
            ),
        }
        issues: list[str] = []
        for criterion, (satisfied, fact_words, fact) in checks.items():
            if criterion not in requested or satisfied:
                continue
            states_fact = any(word.casefold() in reason for word in fact_words)
            marks_tradeoff = any(word.casefold() in reason for word in tradeoff_words)
            if not states_fact or not marks_tradeoff:
                issues.append(fact)
        return issues

    @classmethod
    def _build_grounded_fallback_reason(
        cls,
        selection: AgentCatalogSelection,
        payload: dict[str, object],
    ) -> str:
        """Explain the Agent's top choice using only trusted catalog facts."""

        trusted = cls._trusted_model_products(payload)
        product = trusted.get(selection.selected_skus[0])
        if product is None:
            return selection.reason

        sku = selection.selected_skus[0]
        name = str(product.get("name") or sku)
        price = str(product.get("unit_price") or "unknown")
        keyboard_type = str(product.get("keyboard_type") or "unknown")
        noise_level = str(product.get("noise_level") or "unknown")
        connection = product.get("connection")
        suitable_for = product.get("suitable_for")
        connection_text = ", ".join(
            str(value) for value in connection
        ) if isinstance(connection, list) else str(connection or "unknown")
        suitable_text = ", ".join(
            str(value) for value in suitable_for
        ) if isinstance(suitable_for, list) else str(suitable_for or "unknown")

        requirements = payload.get("reason_requirements")
        if not isinstance(requirements, list):
            requirements = []
        requested = {
            item.get("criterion")
            for item in requirements
            if isinstance(item, dict)
            and isinstance(item.get("criterion"), str)
        }
        facts: list[str] = []
        if "budget" in requested:
            max_price = None
            safety_limits = payload.get("safety_limits")
            if isinstance(safety_limits, dict):
                max_price = safety_limits.get("max_price")
            facts.append(f"价格{price}元，不超过{max_price}元预算")
        if "wireless" in requested:
            supports_wireless = cls._attribute_contains_any(
                connection,
                {"wireless", "bluetooth", "2.4ghz"},
            )
            facts.append(
                f"连接方式为{connection_text}，"
                + ("满足无线连接" if supports_wireless else "不满足无线连接，这是具体取舍")
            )
        if "mechanical" in requested:
            is_mechanical = keyboard_type.casefold() == "mechanical"
            facts.append(
                f"keyboard_type={keyboard_type}，"
                + ("满足机械键盘要求" if is_mechanical else "不满足机械键盘要求，这是具体取舍")
            )
        if "nighttime_low_noise" in requested:
            is_low_noise = noise_level.casefold() in {"low", "silent"}
            facts.append(
                f"noise_level={noise_level}，"
                + (
                    "满足夜间宿舍低噪音需求"
                    if is_low_noise
                    else "不满足夜间宿舍低噪音需求，这是具体取舍"
                )
            )
        if "programming" in requested:
            supports_programming = cls._attribute_contains_any(
                suitable_for,
                {"programming", "coding"},
            )
            facts.append(
                f"适用场景为{suitable_text}，"
                + (
                    "适合写代码或编程"
                    if supports_programming
                    else "目录未明确标注适合写代码或编程，这是具体取舍"
                )
            )
        return f"Agent推荐{sku}（{name}）。目录事实核对：" + "；".join(facts) + "。"

    @staticmethod
    def _attribute_contains_any(actual: object, expected: set[str]) -> bool:
        values = actual if isinstance(actual, list) else [actual]
        normalized = {str(value).strip().casefold() for value in values}
        return bool(normalized & expected)

    @staticmethod
    def _catalog_product_for_model(product: ProductSnapshot) -> list[object]:
        """Project trusted facts into a compact view that fits small local models."""

        core_keys = {
            "keyboard_type",
            "noise_level",
            "connection",
            "suitable_for",
        }
        duplicate_keys = {
            "brand",
            "source_category",
            "suitable_for_zh",
            "feature_tags",
        }
        other_facts: list[object] = []
        for key, value in product.attributes.items():
            if key in core_keys or key in duplicate_keys:
                continue
            values = value if isinstance(value, list) else [value]
            for item in values:
                if item not in other_facts:
                    other_facts.append(item)

        return [
            product.sku,
            product.name,
            str(product.unit_price),
            product.attributes.get("keyboard_type"),
            product.attributes.get("noise_level"),
            product.attributes.get("connection"),
            product.attributes.get("suitable_for"),
            other_facts,
        ]

    def _generate_catalog_selection(
        self,
        payload: dict[str, object],
        messages: list[str],
        *,
        trace_id: str,
        agent_run_id: str,
    ) -> AgentCatalogSelection:
        base_messages = [
            {"role": "system", "content": self._catalog_search_system_prompt},
            {
                "role": "user",
                "content": json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ]
        selection = self.model_gateway.generate(
            base_messages,
            task_type=ModelTaskType.SHOPPING_CATALOG_SEARCH.value,
            response_schema=AgentCatalogSelection,
            prompt_version=self.catalog_search_prompt_version,
            trace_id=trace_id,
            agent_run_id=agent_run_id,
        )
        selection = self._normalize_selected_skus(selection, payload)
        invalid_skus = self._falsely_user_attributed_skus(selection.reason, messages)
        missing_criteria = (
            self._missing_reason_criteria(
                selection.reason,
                payload.get("reason_requirements"),
            )
            if self._has_trusted_catalog_selection(selection, payload)
            else []
        )
        unacknowledged_tradeoffs = (
            self._unacknowledged_primary_tradeoffs(selection, payload)
            if self._has_trusted_catalog_selection(selection, payload)
            else []
        )
        if not invalid_skus and not missing_criteria and not unacknowledged_tradeoffs:
            return selection

        retry_payload = {
            **payload,
            "correction": {
                "error": "recommendation_reason_not_grounded",
                "invalid_skus": invalid_skus,
                "missing_reason_criteria": missing_criteria,
                "unacknowledged_tradeoffs": unacknowledged_tradeoffs,
                "instruction": (
                    "请重新比较目录并改写输出。只能把 messages 当作用户原话；"
                    "invalid_skus 并非用户提供。reason 必须逐项写出 "
                    "missing_reason_criteria 中的每一项，结合所选商品的目录事实说明"
                    "满足情况；并根据 unacknowledged_tradeoffs 中列出的真实属性"
                    "纠正错误结论。若不满足，必须明确写出具体取舍。"
                ),
            },
        }
        selection = self.model_gateway.generate(
            [
                {"role": "system", "content": self._catalog_search_system_prompt},
                {
                    "role": "assistant",
                    "content": selection.model_dump_json(),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        retry_payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            ],
            task_type=ModelTaskType.SHOPPING_CATALOG_SEARCH.value,
            response_schema=AgentCatalogSelection,
            prompt_version=self.catalog_search_prompt_version,
            trace_id=trace_id,
            agent_run_id=agent_run_id,
        )
        selection = self._normalize_selected_skus(selection, payload)
        invalid_skus = self._falsely_user_attributed_skus(selection.reason, messages)
        missing_criteria = (
            self._missing_reason_criteria(
                selection.reason,
                payload.get("reason_requirements"),
            )
            if self._has_trusted_catalog_selection(selection, payload)
            else []
        )
        unacknowledged_tradeoffs = (
            self._unacknowledged_primary_tradeoffs(selection, payload)
            if self._has_trusted_catalog_selection(selection, payload)
            else []
        )
        if invalid_skus:
            details: list[str] = []
            details.append("false user SKU attribution: " + ", ".join(invalid_skus))
            raise InvalidCandidateReferenceError(
                "Recommendation reason remained ungrounded after one correction retry: "
                + "; ".join(details)
            )
        if missing_criteria or unacknowledged_tradeoffs:
            selection = selection.model_copy(
                update={
                    "reason": self._build_grounded_fallback_reason(selection, payload)
                }
            )
        return selection

    def search_catalog(
        self,
        messages: list[str],
        intent: ParsedPurchaseIntent,
        *,
        trace_id: str,
    ) -> GroundedCatalogSearchResult:
        """Read the trusted catalog tool and let the Agent select grounded matches."""

        if not intent.ready_to_search:
            raise ValueError("Catalog search requires a ready purchase intent")
        if not messages or any(not message.strip() for message in messages):
            raise ValueError("Catalog search requires non-empty user messages")
        if self.catalog_tool is None:
            raise ValueError("ShoppingAssistantAgent requires a ProductCatalogTool")

        with self.track_run(
            trace_id,
            action="shopping.catalog_search",
            model_task_type=ModelTaskType.SHOPPING_CATALOG_SEARCH.value,
            prompt_version=self.catalog_search_prompt_version,
        ) as run_context:
            with self.trace_recorder.span(
                trace_id,
                self.catalog_tool.name,
                "catalog.read",
            ):
                catalog = self.catalog_tool.read_catalog()

            # Deterministic code only protects ordering safety boundaries. Product
            # features (mechanical, wireless, quiet, layout, material, and so on)
            # remain Agent-owned semantic choices based on the original wording.
            required_quantity = intent.quantity or 1
            eligible_products = [
                product
                for product in catalog.products
                if product.active
                and product.available_quantity >= required_quantity
                and (
                    intent.max_price is None
                    or product.unit_price <= intent.max_price
                )
                and (
                    intent.category is None
                    or product.category.casefold() == intent.category.casefold()
                )
            ]
            if not eligible_products:
                return GroundedCatalogSearchResult(
                    candidates=[],
                    recommendation=None,
                    no_match_reason=(
                        "No available catalog product satisfies the category, budget, "
                        "and required stock"
                    ),
                )

            selection = self._generate_catalog_selection(
                {
                    "catalog_data_version": catalog.data_version,
                    "catalog_product_columns": list(self._model_catalog_columns),
                    "catalog_products": [
                        self._catalog_product_for_model(product)
                        for product in eligible_products
                    ],
                    # Repeat the user-authored source and explanation checklist after
                    # the potentially long catalog. Small local models otherwise tend
                    # to over-weight products near the end and forget the request.
                    "messages": messages,
                    "safety_limits": {
                        "category": intent.category,
                        "quantity": required_quantity,
                        "max_price": (
                            str(intent.max_price)
                            if intent.max_price is not None
                            else None
                        ),
                    },
                    "reason_requirements": self._build_reason_requirements(
                        messages,
                        intent,
                    ),
                    "catalog_fact_reading_rules": [
                        "逐个SKU独立核对全部条件，不能拼接不同商品的属性",
                        "无线要求connection包含wireless、bluetooth或2.4ghz",
                        "机械要求keyboard_type为mechanical",
                        "低噪音要求noise_level为low或silent；medium和high不满足",
                        "编程适用要求suitable_for包含programming或描述明确支持编程",
                        "如果上述特征有任何一项不满足，reason必须明确写出该项取舍",
                    ],
                },
                messages,
                trace_id=trace_id,
                agent_run_id=run_context.run_id,
            )

        products_by_sku = {product.sku: product for product in eligible_products}
        candidates: list[ProductCandidate] = []
        for sku in selection.selected_skus:
            product = products_by_sku.get(sku)
            if product is None:
                continue
            index = len(candidates)
            candidates.append(
                ProductCandidate(
                    sku=product.sku,
                    name=product.name,
                    category=product.category,
                    unit_price=product.unit_price,
                    available_quantity=product.available_quantity,
                    attributes=product.attributes,
                    matched_preferences=[
                        "Selected by ShoppingAssistantAgent from catalog facts"
                    ],
                    unmet_preferences=[],
                    score=max(0, 100 - index * 10),
                )
            )

        if not candidates:
            return GroundedCatalogSearchResult(
                candidates=[],
                recommendation=None,
                no_match_reason=selection.reason,
            )
        return GroundedCatalogSearchResult(
            candidates=candidates,
            recommendation=ProductRecommendation(
                recommended_sku=candidates[0].sku,
                alternative_skus=[candidate.sku for candidate in candidates[1:]],
                reason=selection.reason,
                tradeoffs=[],
            ),
            no_match_reason=None,
        )

    def rank_candidates(
        self,
        intent: ParsedPurchaseIntent,
        candidates: list[ProductCandidate],
        *,
        trace_id: str,
    ) -> ProductRecommendation:
        if not candidates:
            raise ValueError("Candidate ranking requires at least one trusted product")
        with self.track_run(
            trace_id,
            action="shopping.rank",
            model_task_type=ModelTaskType.SHOPPING_RANK.value,
            prompt_version=self.rank_prompt_version,
        ) as run_context:
            recommendation = self.model_gateway.generate(
                [
                    {
                        "role": "system",
                        "content": (
                            "Compare only the supplied product candidates and return every field "
                            "in the exact supplied schema. recommended_sku must be exactly one "
                            "candidate SKU. alternative_skus may contain only other supplied "
                            "candidate SKUs, without duplicates. reason must explain the best "
                            "match using supplied facts; tradeoffs is a list of factual drawbacks "
                            "or an empty list. Example shape for candidates SKU-A and SKU-B: "
                            "{\"recommended_sku\":\"SKU-A\",\"alternative_skus\":[\"SKU-B\"],"
                            "\"reason\":\"SKU-A best matches the stated preferences\","
                            "\"tradeoffs\":[\"SKU-A costs more than SKU-B\"]}. Replace example "
                            "values with the actual supplied candidates. Never invent product "
                            "facts, attributes, prices, inventory, or SKUs. Return JSON only."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "intent": intent.model_dump(mode="json"),
                                "candidates": [
                                    candidate.model_dump(mode="json")
                                    for candidate in candidates
                                ],
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    },
                ],
                task_type=ModelTaskType.SHOPPING_RANK.value,
                response_schema=ProductRecommendation,
                prompt_version=self.rank_prompt_version,
                trace_id=trace_id,
                agent_run_id=run_context.run_id,
            )

        trusted_skus = {candidate.sku for candidate in candidates}
        referenced_skus = {
            recommendation.recommended_sku,
            *recommendation.alternative_skus,
        }
        untrusted = referenced_skus - trusted_skus
        if untrusted:
            raise InvalidCandidateReferenceError(
                "Recommendation referenced candidate-external SKU(s): "
                + ", ".join(sorted(untrusted))
            )
        return recommendation
