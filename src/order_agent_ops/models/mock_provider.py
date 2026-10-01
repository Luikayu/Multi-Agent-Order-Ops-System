"""Deterministic, configurable model provider for local development and tests."""

import json
import re
import time
from collections.abc import Mapping, Sequence
from copy import deepcopy
from enum import StrEnum
from threading import RLock
from typing import Any

from pydantic import BaseModel

from order_agent_ops.models.provider import ModelMessage, ModelProvider, ModelTaskType


MockTaskType = ModelTaskType


class MockBehavior(StrEnum):
    SUCCESS = "success"
    TIMEOUT = "timeout"
    INVALID_JSON = "invalid_json"
    ERROR = "error"


DEFAULT_RESPONSES: dict[str, dict[str, Any]] = {
    ModelTaskType.SHOPPING_ANALYZE.value: {
        "category": "keyboard",
        "quantity": 1,
        "max_price": "500.00",
        "use_case": "programming",
        "constraint_conflicts": [],
        "clarifying_question": None,
    },
    ModelTaskType.SHOPPING_CATALOG_SEARCH.value: {
        "selected_skus": ["SKU-001"],
        "reason": "The catalog product matches the purchase need",
    },
    ModelTaskType.SHOPPING_RANK.value: {
        "recommended_sku": "SKU-001",
        "alternative_skus": [],
        "reason": "The candidate matches the structured requirements",
        "tradeoffs": [],
    },
    ModelTaskType.ORDER_COORDINATOR.value: {
        "request_valid": True,
        "required_checks": ["inventory", "risk"],
        "reason": "Required order fields are present",
    },
    ModelTaskType.INVENTORY_AGENT.value: {
        "sku": "SKU-001",
        "requested_quantity": 1,
        "status": "sufficient",
        "available_quantity": 10,
        "reason": "Mock inventory snapshot is sufficient",
        "evidence_ids": [],
    },
    ModelTaskType.RISK_AGENT.value: {
        "user_id": "USER-001",
        "risk_score": 0.2,
        "risk_level": "low",
        "reason": "Mock risk profile is stable",
        "evidence_ids": [],
        "requires_manual_review": False,
    },
    ModelTaskType.OPS_GUARDIAN.value: {
        "incident_id": "INC-MOCK-001",
        "facts": [
            {
                "statement": "Inventory latency exceeded the configured threshold",
                "evidence_ids": ["E-MOCK-001"],
            }
        ],
        "root_cause_candidates": [
            {
                "cause": "Inventory agent v2.1 latency regression",
                "confidence": 0.82,
                "evidence_ids": ["E-MOCK-001"],
            }
        ],
        "missing_information": [],
        "recommended_actions": [
            {
                "action": "rollback",
                "target": "inventory-agent",
                "risk_level": "high",
                "requires_approval": True,
                "evidence_ids": ["E-MOCK-001"],
            }
        ],
    },
}


class MockModelProvider(ModelProvider):
    """Return fixed JSON and consume an optional behavior sequence per task."""

    def __init__(
        self,
        *,
        model_name: str = "mock-order-model",
        responses: Mapping[str, Mapping[str, Any]] | None = None,
        behaviors: Mapping[str, Sequence[MockBehavior | str]] | None = None,
        latency_seconds: float = 0.0,
    ) -> None:
        if not model_name.strip():
            raise ValueError("Mock model name must not be empty")
        if latency_seconds < 0:
            raise ValueError("Mock latency must not be negative")

        self._model_name = model_name.strip()
        self._responses = deepcopy(DEFAULT_RESPONSES)
        self._custom_response_tasks = set(responses or {})
        if responses:
            self._responses.update(
                {task: deepcopy(dict(response)) for task, response in responses.items()}
            )
        self._behaviors = {
            task: [MockBehavior(behavior) for behavior in sequence]
            for task, sequence in (behaviors or {}).items()
        }
        self._latency_seconds = latency_seconds
        self._calls: list[str] = []
        self._lock = RLock()

    @property
    def provider_name(self) -> str:
        return "mock"

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def calls(self) -> list[str]:
        with self._lock:
            return list(self._calls)

    def generate(
        self,
        messages: Sequence[ModelMessage],
        *,
        task_type: str,
        response_schema: type[BaseModel] | None = None,
    ) -> str:
        del response_schema
        with self._lock:
            self._calls.append(task_type)
            sequence = self._behaviors.get(task_type)
            behavior = sequence.pop(0) if sequence else MockBehavior.SUCCESS

        if self._latency_seconds:
            time.sleep(self._latency_seconds)

        if behavior is MockBehavior.TIMEOUT:
            raise TimeoutError("Simulated mock provider timeout")
        if behavior is MockBehavior.INVALID_JSON:
            return "{this-is-not-valid-json"
        if behavior is MockBehavior.ERROR:
            raise RuntimeError("Simulated mock provider failure")

        try:
            if (
                task_type == ModelTaskType.SHOPPING_ANALYZE.value
                and task_type not in self._custom_response_tasks
            ):
                response = self._analyze_shopping_messages(messages)
            elif (
                task_type == ModelTaskType.SHOPPING_CATALOG_SEARCH.value
                and task_type not in self._custom_response_tasks
            ):
                response = self._search_shopping_catalog(messages)
            elif (
                task_type == ModelTaskType.SHOPPING_RANK.value
                and task_type not in self._custom_response_tasks
            ):
                response = self._rank_shopping_candidates(messages)
            elif (
                task_type == ModelTaskType.OPS_GUARDIAN.value
                and task_type not in self._custom_response_tasks
            ):
                response = self._diagnose_ops_messages(messages)
            else:
                response = self._responses[task_type]
        except KeyError as error:
            raise KeyError(f"No mock response configured for task: {task_type}") from error
        return json.dumps(response, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _last_user_payload(messages: Sequence[ModelMessage]) -> dict[str, Any]:
        for message in reversed(messages):
            if message.get("role") != "user":
                continue
            try:
                payload = json.loads(message.get("content", ""))
            except json.JSONDecodeError:
                return {"messages": [message.get("content", "")]}
            if isinstance(payload, dict):
                return payload
        return {"messages": []}

    @classmethod
    def _analyze_shopping_messages(
        cls, messages: Sequence[ModelMessage]
    ) -> dict[str, Any]:
        payload = cls._last_user_payload(messages)
        raw_messages = payload.get("messages", [])
        if not isinstance(raw_messages, list):
            raw_messages = [str(raw_messages)]
        text = " ".join(str(item) for item in raw_messages)
        lowered = text.casefold()

        category = None
        if "键盘" in text or "keyboard" in lowered:
            category = "keyboard"
        elif "鼠标" in text or "mouse" in lowered:
            category = "mouse"
        elif "耳机" in text or "headset" in lowered:
            category = "headset"
        elif "支架" in text or "stand" in lowered:
            category = "laptop_stand"

        quantity = None
        quantity_match = re.search(r"(\d+)\s*(?:个|把|件|台|只)", text)
        if quantity_match:
            quantity = int(quantity_match.group(1))
        elif any(token in text for token in ("一个", "一把", "一件", "一只")):
            quantity = 1

        max_price = None
        price_patterns = (
            r"(?:预算|不超过|低于|最多)\s*(\d+(?:\.\d+)?)",
            r"(\d+(?:\.\d+)?)\s*元?(?:以内|以下|左右)",
        )
        for pattern in price_patterns:
            price_match = re.search(pattern, text)
            if price_match:
                max_price = price_match.group(1)
                break

        use_case = None
        if "宿舍" in text:
            use_case = "dormitory"
        elif "编程" in text or "写代码" in text:
            use_case = "programming"
        elif "办公" in text:
            use_case = "office"
        elif "出差" in text or "旅行" in text:
            use_case = "travel"
        elif "游戏" in text:
            use_case = "gaming"

        conflicts: list[str] = []
        if (
            max_price is not None
            and float(max_price) <= 150
            and "旗舰" in text
            and ("全铝" in text or "铝合金" in text)
            and "无线" in text
        ):
            conflicts.append(
                "A budget of 150 or less conflicts with flagship, aluminum, and wireless requirements"
            )

        missing: list[str] = []
        if category is None:
            missing.append("product_category")
        if quantity is None:
            missing.append("quantity")
        if max_price is None:
            missing.append("budget")
        question = None
        if conflicts:
            question = "这些条件与预算冲突，你希望优先保留预算、无线、全铝还是旗舰定位？"
        elif missing:
            labels = "、".join(missing)
            question = f"请补充以下信息：{labels}。"

        return {
            "category": category,
            "quantity": quantity,
            "max_price": max_price,
            "use_case": use_case,
            "constraint_conflicts": conflicts,
            "clarifying_question": question,
        }

    @staticmethod
    def _attribute_contains(actual: Any, expected: str) -> bool:
        normalized = expected.strip().casefold()
        if isinstance(actual, list):
            return any(str(value).strip().casefold() == normalized for value in actual)
        return str(actual).strip().casefold() == normalized

    @classmethod
    def _search_shopping_catalog(
        cls, messages: Sequence[ModelMessage]
    ) -> dict[str, Any]:
        """Simulate Agent-owned semantic retrieval for deterministic offline tests."""

        payload = cls._last_user_payload(messages)
        safety_limits = payload.get("safety_limits", {})
        products = payload.get("catalog_products", [])
        product_columns = payload.get("catalog_product_columns", [])
        raw_messages = payload.get("messages", [])
        if not isinstance(safety_limits, dict) or not isinstance(products, list):
            raise KeyError(
                "Shopping catalog search requires safety limits and catalog products"
            )
        if not isinstance(raw_messages, list):
            raw_messages = [str(raw_messages)]
        text = " ".join(str(item) for item in raw_messages)
        lowered = text.casefold()

        quantity = int(safety_limits.get("quantity") or 1)
        category = str(safety_limits.get("category") or "").casefold()
        max_price = safety_limits.get("max_price")
        max_price_value = float(max_price) if max_price is not None else None

        ranked: list[tuple[int, float, str, dict[str, Any]]] = []
        for raw_product in products:
            product = raw_product
            if isinstance(raw_product, list) and isinstance(product_columns, list):
                product = dict(zip(product_columns, raw_product, strict=False))
            if not isinstance(product, dict):
                continue
            attributes = product.get("attributes")
            if not isinstance(attributes, dict):
                attributes = {
                    "keyboard_type": product.get("keyboard_type"),
                    "noise_level": product.get("noise_level"),
                    "connection": product.get("connection"),
                    "suitable_for": product.get("suitable_for"),
                    "other_facts": product.get("other_facts", []),
                }
            if not isinstance(attributes, dict):
                attributes = {}
            # The real Agent receives a compact view after deterministic safety
            # filtering. Keep accepting the older full view for compatibility.
            if "active" in product and not product.get("active"):
                continue
            if (
                "available_quantity" in product
                and int(product.get("available_quantity") or 0) < quantity
            ):
                continue
            if (
                category
                and "category" in product
                and str(product.get("category") or "").casefold() != category
            ):
                continue
            price = float(product.get("unit_price") or 0)
            if max_price_value is not None and price > max_price_value:
                continue

            score = 0
            feature_checks = (
                (
                    "机械" in text or "mechanical" in lowered,
                    cls._attribute_contains(
                        attributes.get("keyboard_type"), "mechanical"
                    ),
                    30,
                ),
                (
                    "无线" in text or "wireless" in lowered,
                    cls._attribute_contains(attributes.get("connection"), "wireless"),
                    30,
                ),
                (
                    any(
                        token in text
                        for token in ("不影响室友", "别影响室友", "安静", "静音")
                    )
                    or ("晚上" in text and "宿舍" in text),
                    str(attributes.get("noise_level") or "").casefold()
                    in {"silent", "low"},
                    25,
                ),
                (
                    "全铝" in text or "铝合金" in text,
                    cls._attribute_contains(attributes.get("material"), "aluminum")
                    or cls._attribute_contains(
                        attributes.get("other_facts"), "aluminum"
                    ),
                    20,
                ),
                (
                    "宿舍" in text,
                    cls._attribute_contains(
                        attributes.get("suitable_for"), "dormitory"
                    ),
                    15,
                ),
                (
                    "写代码" in text or "编程" in text or "programming" in lowered,
                    cls._attribute_contains(
                        attributes.get("suitable_for"), "programming"
                    ),
                    15,
                ),
                (
                    "出差" in text or "旅行" in text or "travel" in lowered,
                    cls._attribute_contains(attributes.get("weight_class"), "light")
                    or cls._attribute_contains(attributes.get("other_facts"), "light"),
                    15,
                ),
            )
            for requested, matched, weight in feature_checks:
                if requested:
                    score += weight if matched else -weight

            ranked.append((score, price, str(product.get("sku") or ""), product))

        ranked.sort(key=lambda item: (-item[0], item[1], item[2]))
        selected = ranked[:5]
        if not selected:
            return {
                "selected_skus": [],
                "reason": (
                    "No available catalog product satisfies the category, budget, "
                    "and required stock"
                ),
            }

        reason_requirements = payload.get("reason_requirements", [])
        reason_descriptions = [
            requirement["description"]
            for requirement in reason_requirements
            if isinstance(requirement, dict)
            and isinstance(requirement.get("description"), str)
        ] if isinstance(reason_requirements, list) else []
        reason = (
            "已根据原始需求和可信目录事实综合比较："
            + "、".join(reason_descriptions)
            if reason_descriptions
            else (
                "Best semantic matches selected directly from the original request "
                "and trusted catalog facts"
            )
        )

        return {
            "selected_skus": [item[3]["sku"] for item in selected],
            "reason": reason,
        }

    @classmethod
    def _rank_shopping_candidates(
        cls, messages: Sequence[ModelMessage]
    ) -> dict[str, Any]:
        payload = cls._last_user_payload(messages)
        candidates = payload.get("candidates", [])
        if not isinstance(candidates, list) or not candidates:
            raise KeyError("Shopping rank requires at least one candidate")
        recommended = candidates[0]
        alternatives = [item["sku"] for item in candidates[1:3]]
        matched = recommended.get("matched_preferences", [])
        reason = "Best deterministic candidate score"
        if matched:
            reason += ": " + "; ".join(str(item) for item in matched)
        return {
            "recommended_sku": recommended["sku"],
            "alternative_skus": alternatives,
            "reason": reason,
            "tradeoffs": recommended.get("unmet_preferences", []),
        }

    @classmethod
    def _diagnose_ops_messages(
        cls, messages: Sequence[ModelMessage]
    ) -> dict[str, Any]:
        payload = cls._last_user_payload(messages)
        incident = payload.get("incident", {})
        evidence = payload.get("evidence", [])
        if (
            not isinstance(incident, dict)
            or "incident_id" not in incident
            or "evidence" not in payload
        ):
            return deepcopy(DEFAULT_RESPONSES[ModelTaskType.OPS_GUARDIAN.value])
        if not isinstance(evidence, list):
            evidence = []

        incident_id = incident.get("incident_id", "INC-MOCK-UNKNOWN")
        by_type: dict[str, list[dict[str, Any]]] = {}
        for item in evidence:
            if not isinstance(item, dict):
                continue
            source = item.get("source", {})
            source_type = source.get("type") if isinstance(source, dict) else None
            evidence_id = item.get("evidence_id")
            if isinstance(source_type, str) and isinstance(evidence_id, str):
                by_type.setdefault(source_type, []).append(item)

        metric = by_type.get("metric", [])
        trace = by_type.get("trace", [])
        v21_deployments = [
            item
            for item in by_type.get("deployment", [])
            if isinstance(item.get("object"), dict)
            and item["object"].get("version") == "v2.1"
        ]
        required_items = [
            metric[0] if metric else None,
            trace[0] if trace else None,
            v21_deployments[0] if v21_deployments else None,
        ]
        complete = all(item is not None for item in required_items)

        facts: list[dict[str, Any]] = []
        for source_type in ("metric", "log", "trace", "deployment"):
            items = by_type.get(source_type, [])
            if not items:
                continue
            item = items[0]
            fact = item.get("fact", {})
            observation = (
                fact.get("observation")
                if isinstance(fact, dict)
                else f"{source_type} evidence was collected"
            )
            facts.append(
                {
                    "statement": str(observation),
                    "evidence_ids": [item["evidence_id"]],
                }
            )

        if complete:
            root_evidence_ids = [item["evidence_id"] for item in required_items]
            return {
                "incident_id": incident_id,
                "facts": facts,
                "root_cause_candidates": [
                    {
                        "cause": (
                            "inventory-agent v2.1 introduced an inventory "
                            "latency regression"
                        ),
                        "confidence": 0.9,
                        "evidence_ids": root_evidence_ids,
                    }
                ],
                "missing_information": [],
                "recommended_actions": [
                    {
                        "action": "rollback",
                        "target": "inventory-agent",
                        "risk_level": "high",
                        "requires_approval": True,
                        "evidence_ids": root_evidence_ids,
                    }
                ],
            }

        missing: list[str] = []
        if not metric:
            missing.append("inventory-agent latency metric evidence")
        if not trace:
            missing.append("inventory-agent trace evidence")
        if not v21_deployments:
            missing.append("inventory-agent v2.1 deployment evidence")
        return {
            "incident_id": incident_id,
            "facts": facts,
            "root_cause_candidates": [],
            "missing_information": missing,
            "recommended_actions": [],
        }
