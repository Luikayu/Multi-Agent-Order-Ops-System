from scripts.shopping_workflow_renderer import render_workflow_error


def test_pretty_error_output_identifies_stage_without_json_noise() -> None:
    rendered = render_workflow_error(
        {
            "status": "failed",
            "stage": "create_purchase_intent",
            "error": "Model endpoint timed out",
        }
    )

    assert "自然语言购物工作流 · 执行失败" in rendered
    assert "create_purchase_intent" in rendered
    assert "Model endpoint timed out" in rendered
    assert '"status"' not in rendered
