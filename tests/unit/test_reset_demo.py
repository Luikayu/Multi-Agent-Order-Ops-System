from pathlib import Path

from scripts.reset_demo import reset_demo_environment


def test_reset_removes_numbered_workflow_artifacts_but_preserves_other_files(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "demo.sqlite3"
    artifacts_root = tmp_path / "artifacts"
    reports = artifacts_root / "reports"
    traces = artifacts_root / "traces"
    reports.mkdir(parents=True)
    traces.mkdir(parents=True)

    database_path.write_bytes(b"demo")
    numbered_report = reports / "shopping_workflow_report_001.json"
    numbered_trace = traces / "shopping_workflow_trace_001.json"
    unrelated = reports / "keep_me.json"
    numbered_report.write_text("{}", encoding="utf-8")
    numbered_trace.write_text("{}", encoding="utf-8")
    unrelated.write_text("{}", encoding="utf-8")

    summary = reset_demo_environment(
        database_path,
        artifacts_root,
        clear_artifacts=True,
    )

    assert summary.database_removed is True
    assert not database_path.exists()
    assert not numbered_report.exists()
    assert not numbered_trace.exists()
    assert unrelated.is_file()
    assert set(summary.artifacts_removed) == {
        "reports/shopping_workflow_report_001.json",
        "traces/shopping_workflow_trace_001.json",
    }
