import json
from pathlib import Path

from scripts.run_happy_path import run_standalone_happy_path
from scripts.shopping_workflow_renderer import render_shopping_workflow


SEMANTIC_TEST_MESSAGE = (
    "我想买一个500元以内、晚上在宿舍写代码、别影响室友的机械键盘"
)


def test_natural_language_happy_path_is_repeatable_and_exports_trace(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "happy.sqlite3"
    artifacts_root = tmp_path / "artifacts"

    first = run_standalone_happy_path(
        database_path=database_path,
        artifacts_root=artifacts_root,
        message=SEMANTIC_TEST_MESSAGE,
    )
    second = run_standalone_happy_path(
        database_path=database_path,
        artifacts_root=artifacts_root,
        message=SEMANTIC_TEST_MESSAGE,
    )

    for result in (first, second):
        assert result["natural_language_need"] == SEMANTIC_TEST_MESSAGE
        assert result["structured_intent"]["ready_to_search"] is True
        assert result["candidates"]
        assert result["candidates"][0]["attributes"]["noise_level"] in {
            "silent",
            "low",
        }
        assert result["confirmation"]["server_unit_price"] == result["order"][
            "total_amount"
        ]
        assert result["order"]["status"] == "COMPLETED"
        assert result["trace_id"].startswith("TRACE-")
        assert {
            "shopping-assistant-agent",
            "order-coordinator-agent",
            "inventory-agent",
            "risk-agent",
        }.issubset(result["agent_execution_order"])
        assert result["total_duration_ms"] >= 0

    first_report_path = (
        artifacts_root / "reports" / "shopping_workflow_report_001.json"
    )
    first_trace_path = artifacts_root / "traces" / "shopping_workflow_trace_001.json"
    second_report_path = (
        artifacts_root / "reports" / "shopping_workflow_report_002.json"
    )
    second_trace_path = artifacts_root / "traces" / "shopping_workflow_trace_002.json"
    assert first_report_path.is_file()
    assert first_trace_path.is_file()
    assert second_report_path.is_file()
    assert second_trace_path.is_file()

    first_report = json.loads(first_report_path.read_text(encoding="utf-8"))
    first_trace = json.loads(first_trace_path.read_text(encoding="utf-8"))
    second_report = json.loads(second_report_path.read_text(encoding="utf-8"))
    second_trace = json.loads(second_trace_path.read_text(encoding="utf-8"))

    assert first_report["scenario"] == "natural_language_shopping_workflow"
    assert first_report["artifact_sequence"] == "001"
    assert first_trace["trace_id"] == first["trace_id"]
    assert second_report["scenario"] == "natural_language_shopping_workflow"
    assert second_report["artifact_sequence"] == "002"
    assert second_report["order"]["status"] == "COMPLETED"
    assert second_trace["trace_id"] == second["trace_id"]
    assert first["artifacts"]["report"].endswith("shopping_workflow_report_001.json")
    assert second["artifacts"]["report"].endswith("shopping_workflow_report_002.json")

    for report_path in (first_report_path, second_report_path):
        exported = report_path.read_text(encoding="utf-8")
        assert "APPROVAL-TOKEN-" not in exported
        assert "api_key" not in exported.lower()

    rendered = render_shopping_workflow(second)
    assert "ShoppingAssistantAgent · 购买需求分析" in rendered
    assert "ProductCatalogTool · 读取受信任商品目录" in rendered
    assert "ShoppingAssistantAgent · Agent 检索与候选排序" in rendered
    assert "OrderCoordinatorAgent · 订单任务协调" in rendered
    assert "InventoryAgent · 库存检查" in rendered
    assert "RiskAgent · 用户风险检查" in rendered
    assert "OrderPolicyEngine · 确定性订单决策" in rendered
    assert "OrderExecutorService · 创建订单" in rendered
    assert "分析摘要（基于结构化输出，不展示原始思维链）" in rendered
    assert "shopping_workflow_report_002.json" in rendered
