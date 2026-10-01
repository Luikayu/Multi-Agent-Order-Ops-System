"""Human-readable terminal rendering for the shopping workflow demo."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


WIDTH = 72
HEAVY_LINE = "═" * WIDTH
LIGHT_LINE = "─" * WIDTH


def _text(value: Any, default: str = "未提供") -> str:
    if value is None or value == "":
        return default
    return str(value)


def _money(value: Any) -> str:
    if value is None or value == "":
        return "未限制"
    try:
        return f"¥{float(value):.2f}"
    except (TypeError, ValueError):
        return f"¥{value}"


def _duration(value: Any) -> str:
    try:
        milliseconds = float(value)
    except (TypeError, ValueError):
        return "未记录"
    if milliseconds >= 1000:
        return f"{milliseconds / 1000:.2f} 秒"
    return f"{milliseconds:.2f} 毫秒"


def _section(number: int, title: str, status: str = "✓ 完成") -> list[str]:
    return ["", f"[{number}/9] {title}    {status}", LIGHT_LINE]


def _span_duration(
    result: Mapping[str, Any],
    component: str,
    action: str | None = None,
) -> Any:
    for span in result.get("execution_trace", []):
        if span.get("component") != component:
            continue
        if action is not None and span.get("action") != action:
            continue
        return span.get("duration_ms")
    return None


def _format_values(values: Any) -> str:
    if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
        return " / ".join(str(value) for value in values)
    return _text(values)


def _requirement_lines(
    requirements: Sequence[Mapping[str, Any]],
    *,
    marker: str,
) -> list[str]:
    lines: list[str] = []
    for requirement in requirements:
        attribute = _text(requirement.get("attribute"))
        values = _format_values(requirement.get("acceptable_values"))
        reason = requirement.get("reason")
        suffix = f"；原因：{reason}" if reason else ""
        lines.append(f"  {marker} {attribute} = {values}{suffix}")
    return lines


def _candidate_attribute_summary(candidate: Mapping[str, Any]) -> str:
    attributes = candidate.get("attributes") or {}
    selected: list[str] = []
    for key, label in (
        ("keyboard_type", "类型"),
        ("connection", "连接"),
        ("noise_level", "噪音"),
        ("layout", "布局"),
    ):
        value = attributes.get(key)
        if value is not None:
            selected.append(f"{label}={_format_values(value)}")
    return "，".join(selected) if selected else "详见结构化报告"


def render_shopping_workflow(result: Mapping[str, Any]) -> str:
    """Render one successful workflow result without inventing unavailable facts."""

    sequence = _text(result.get("artifact_sequence"), "---")
    model = result.get("model") or {}
    structured = result.get("structured_intent") or {}
    candidates = result.get("candidates") or []
    recommendation = result.get("recommendation") or {}
    confirmation = result.get("confirmation") or {}
    order = result.get("order") or {}
    artifacts = result.get("artifacts") or {}
    recommended_sku = recommendation.get("recommended_sku")
    selected = next(
        (candidate for candidate in candidates if candidate.get("sku") == recommended_sku),
        {},
    )

    lines = [
        HEAVY_LINE,
        f"自然语言购物工作流 · 第 {sequence} 次运行",
        HEAVY_LINE,
        "",
        f"运行模式     {_text(model.get('provider'))} / {_text(model.get('name'))}",
        "用户         USER-001",
        f"Trace ID     {_text(result.get('trace_id'))}",
        "",
        "用户需求",
        LIGHT_LINE,
        f"“{_text(result.get('natural_language_need'))}”",
    ]

    lines.extend(_section(1, "ShoppingAssistantAgent · 购买需求分析"))
    lines.extend(
        [
            "组件类型     LLM Agent",
            "任务         analyze_purchase_intent",
            f"模型         {_text(model.get('name'))}",
            f"Prompt       {_text(model.get('analyze_prompt_version'))}",
            "",
            "分析摘要（基于结构化输出，不展示原始思维链）",
            f"  • 商品类别：{_text(structured.get('category'))}",
            f"  • 购买数量：{_text(structured.get('quantity'))}",
            f"  • 最高预算：{_money(structured.get('max_price'))}",
            f"  • 使用场景：{_text(structured.get('use_case'))}",
        ]
    )
    inferred = structured.get("inferred_requirements") or []
    for requirement in inferred:
        lines.append(
            "  • 推断要求："
            f"{_text(requirement.get('attribute'))} = "
            f"{_text(requirement.get('value'))}；"
            f"原因：{_text(requirement.get('reason'))}"
        )
    lines.extend(["", "硬性条件"])
    hard_lines = _requirement_lines(
        structured.get("hard_constraints") or [], marker="✓"
    )
    lines.extend(hard_lines or ["  （无）"])
    lines.extend(["", "软偏好"])
    soft_lines = _requirement_lines(
        structured.get("soft_preferences") or [], marker="○"
    )
    lines.extend(soft_lines or ["  （无）"])
    lines.extend(
        [
            "",
            "校验",
            "  ✓ JSON 解析成功",
            "  ✓ ParsedPurchaseIntent 校验通过",
            "  ✓ 信息完整，可以搜索商品",
            "",
            "耗时         "
            + _duration(
                _span_duration(
                    result, "shopping-assistant-agent", "shopping.analyze"
                )
            ),
        ]
    )

    lines.extend(_section(2, "ProductCatalogTool · 读取受信任商品目录"))
    lines.extend(
        [
            "组件类型     Agent 的只读业务工具",
            "数据来源     本地受信任商品目录",
            "",
            "工具边界",
            "  ✓ 读取目录中的 SKU、名称、价格、库存、描述和扩展属性",
            "  ✓ 不做语义匹配、不打分、不创建商品事实",
            "  ✓ 目录快照已交给 ShoppingAssistantAgent",
        ]
    )
    lines.extend(
        [
            "",
            "耗时         "
            + _duration(
                _span_duration(result, "product-catalog-tool", "catalog.read")
            ),
        ]
    )

    lines.extend(_section(3, "ShoppingAssistantAgent · Agent 检索与候选排序"))
    lines.extend(
        [
            "组件类型     LLM Agent",
            "任务         读取目录后进行语义检索、约束判断和候选排序",
            f"模型         {_text(model.get('name'))}",
            "Prompt       "
            + _text(
                model.get("catalog_search_prompt_version")
                or model.get("rank_prompt_version")
            ),
            "",
            "检索依据",
            "  ✓ 同时读取用户原始表达和结构化需求",
            "  ✓ 综合商品描述与不同商品的扩展属性，不只做关键字相等匹配",
            "  ✓ SKU、价格、库存和属性仍以目录快照为准",
            "",
            f"Agent 候选   {len(candidates)} 件商品",
        ]
    )
    for index, candidate in enumerate(candidates, start=1):
        lines.extend(
            [
                "",
                f"  {index}. {_text(candidate.get('sku'))}  "
                f"{_text(candidate.get('name'))}",
                f"     价格 {_money(candidate.get('unit_price'))}  "
                f"库存 {_text(candidate.get('available_quantity'))}  "
                f"评分 {_text(candidate.get('score'))}",
                f"     属性：{_candidate_attribute_summary(candidate)}",
            ]
        )
        matched = candidate.get("matched_preferences") or []
        unmet = candidate.get("unmet_preferences") or []
        if matched:
            lines.append(f"     已匹配：{'；'.join(str(item) for item in matched)}")
        if unmet:
            lines.append(f"     未满足：{'；'.join(str(item) for item in unmet)}")
    lines.extend(
        [
            "",
            f"推荐商品     ★ {_text(recommended_sku)} · {_text(selected.get('name'))}",
            f"服务端价格   {_money(selected.get('unit_price'))}",
            "",
            "模型推荐说明（仅作解释，商品事实以上方目录数据为准）",
            f"  {_text(recommendation.get('reason'))}",
        ]
    )
    alternatives = recommendation.get("alternative_skus") or []
    if alternatives:
        lines.extend(["", f"备选商品     {', '.join(str(item) for item in alternatives)}"])
    tradeoffs = recommendation.get("tradeoffs") or []
    if tradeoffs:
        lines.extend(["", "模型给出的取舍说明"])
        lines.extend(f"  • {item}" for item in tradeoffs)
    lines.extend(
        [
            "",
            "安全校验",
            "  ✓ 推荐 SKU 存在于服务端候选集合",
            "  ✓ 推荐价格来自受信任商品目录",
            "",
            "耗时         "
            + _duration(
                _span_duration(
                    result,
                    "shopping-assistant-agent",
                    "shopping.catalog_search",
                )
            ),
        ]
    )

    lines.extend(_section(4, "用户确认 · 商品确认边界", "✓ 已确认"))
    lines.extend(
        [
            "确认方式     演示脚本自动确认推荐商品",
            f"意图编号     {_text(confirmation.get('intent_id'))}",
            f"确认商品     {_text(confirmation.get('sku'))} · {_text(selected.get('name'))}",
            f"购买数量     {_text(confirmation.get('quantity'))}",
            f"服务端价格   {_money(confirmation.get('server_unit_price'))}",
            "",
            "服务端复核",
            "  ✓ SKU 属于当前候选集合",
            "  ✓ 重新读取当前商品与价格",
            "  ✓ 由服务端计算订单金额",
        ]
    )

    lines.extend(_section(5, "OrderCoordinatorAgent · 订单任务协调"))
    lines.extend(
        [
            "组件类型     LLM Agent",
            "任务         校验订单并调度库存与风险检查",
            "执行结果",
            "  ✓ InventoryAgent 已执行库存检查",
            "  ✓ RiskAgent 已执行风险检查",
            "  ✓ 检查结果已交给确定性订单策略",
            "",
            "耗时         "
            + _duration(_span_duration(result, "order-coordinator-agent")),
        ]
    )

    lines.extend(_section(6, "InventoryAgent · 库存检查", "✓ 通过"))
    lines.extend(
        [
            "组件类型     LLM Agent + 只读库存适配器",
            f"检查商品     {_text(confirmation.get('sku'))}",
            f"请求数量     {_text(confirmation.get('quantity'))}",
            f"搜索时库存   {_text(selected.get('available_quantity'))}",
            "可见结论     库存检查完成，订单策略允许流程继续",
            "说明         当前确认 API 未导出 InventoryAgent 的完整原始结果",
            "注意         InventoryAgent 只判断库存，不直接扣减库存",
            "",
            "耗时         " + _duration(_span_duration(result, "inventory-agent")),
        ]
    )

    lines.extend(_section(7, "RiskAgent · 用户风险检查", "✓ 通过"))
    lines.extend(
        [
            "组件类型     LLM Agent + 只读风险档案适配器",
            "检查用户     USER-001",
            "可见结论     风险检查完成，订单未进入人工审核",
            "说明         当前确认 API 未导出具体风险分数和风险理由",
            "注意         RiskAgent 只提供判断，不直接创建或拒绝订单",
            "",
            "耗时         " + _duration(_span_duration(result, "risk-agent")),
        ]
    )

    lines.extend(_section(8, "OrderPolicyEngine · 确定性订单决策", "✓ 允许创建"))
    lines.extend(
        [
            "组件类型     确定性规则，不调用模型",
            "可见决策     库存与风险检查通过，允许执行订单创建",
            "",
            "耗时         " + _duration(_span_duration(result, "order-policy")),
        ]
    )

    lines.extend(_section(9, "OrderExecutorService · 创建订单", "✓ 完成"))
    lines.extend(
        [
            "组件类型     确定性事务服务",
            "执行结果",
            "  ✓ 幂等关系由服务端检查",
            "  ✓ 库存预留和订单写入在事务中完成",
            "  ✓ 状态变化写入审计记录",
            "",
            f"订单编号     {_text(order.get('order_id'))}",
            f"商品         {_text(selected.get('name'))}",
            f"数量         {_text(confirmation.get('quantity'))}",
            f"总金额       {_money(order.get('total_amount'))}",
            f"最终状态     {_text(order.get('status'))}",
            "",
            "耗时         " + _duration(_span_duration(result, "order-executor")),
        ]
    )

    agent_order = result.get("agent_execution_order") or []
    model_calls = sum(
        1
        for span in result.get("execution_trace", [])
        if span.get("component") == "model-gateway"
    )
    ops_triggered = "ops-guardian-agent" in agent_order
    lines.extend(
        [
            "",
            HEAVY_LINE,
            "执行摘要",
            HEAVY_LINE,
            "",
            f"最终结果     {'✓' if order.get('status') == 'COMPLETED' else '•'} "
            f"{_text(order.get('status'))}",
            f"推荐商品     {_text(selected.get('name'))}",
            f"服务端价格   {_money(confirmation.get('server_unit_price'))}",
            f"业务 Agent   {len(agent_order)} 个",
            f"模型调用     {model_calls} 次",
            f"总耗时       {_duration(result.get('total_duration_ms'))}",
            "运维诊断     "
            + ("OpsGuardianAgent 已触发" if ops_triggered else "未检测到异常，未触发"),
            "",
            "报告文件",
            f"  {_text(artifacts.get('report'))}",
            "调用链文件",
            f"  {_text(artifacts.get('trace'))}",
        ]
    )
    return "\n".join(lines)


def render_workflow_error(payload: Mapping[str, Any]) -> str:
    """Render a concise stage-labelled failure for interactive CLI users."""

    return "\n".join(
        [
            HEAVY_LINE,
            "自然语言购物工作流 · 执行失败",
            HEAVY_LINE,
            "",
            f"失败阶段     {_text(payload.get('stage'))}",
            f"错误信息     {_text(payload.get('error'))}",
            "",
            "未生成成功订单。请根据失败阶段检查服务、模型输出或请求条件。",
        ]
    )
