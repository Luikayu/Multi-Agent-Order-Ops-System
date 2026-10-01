import pytest

from order_agent_ops.telemetry.metrics import MetricRegistry


def test_metrics_calculate_counts_rates_and_nearest_rank_p95() -> None:
    metrics = MetricRegistry()

    for duration in range(1, 101):
        metrics.record(
            "inventory.check",
            duration,
            success=duration not in {25, 50, 75, 100},
        )

    summary = metrics.summary("inventory.check")

    assert summary.count == 100
    assert summary.error_count == 4
    assert summary.success_count == 96
    assert summary.success_rate == pytest.approx(0.96)
    assert summary.error_rate == pytest.approx(0.04)
    assert summary.p95_duration_ms == 95


def test_unknown_metric_has_empty_summary() -> None:
    summary = MetricRegistry().summary("unknown.operation")

    assert summary.count == 0
    assert summary.error_count == 0
    assert summary.success_rate == 0
    assert summary.p95_duration_ms is None


@pytest.mark.parametrize(
    ("name", "duration"),
    [("", 1), ("inventory.check", -1)],
)
def test_invalid_metric_input_is_rejected(name: str, duration: float) -> None:
    with pytest.raises(ValueError):
        MetricRegistry().record(name, duration, success=True)


def test_snapshot_and_clear_are_isolated_from_internal_state() -> None:
    metrics = MetricRegistry()
    metrics.record("order.request", 20, success=True)

    snapshot = metrics.snapshot()
    metrics.clear()

    assert snapshot["order.request"].count == 1
    assert metrics.summary("order.request").count == 0
