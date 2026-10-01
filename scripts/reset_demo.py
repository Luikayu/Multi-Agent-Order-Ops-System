"""Reset only the local state owned by the repeatable demonstration scripts."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from order_agent_ops.config import PROJECT_ROOT

if __package__:
    from scripts.demo_support import DEFAULT_ARTIFACTS_ROOT, configure_utf8_stdout
else:  # Allow `python scripts/reset_demo.py` from the project root.
    from demo_support import DEFAULT_ARTIFACTS_ROOT, configure_utf8_stdout


DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "demo.sqlite3"
GENERATED_ARTIFACTS = (
    Path("traces/happy_path_trace.json"),
    Path("traces/inventory_latency_incident_traces.json"),
    Path("reports/happy_path_report.json"),
    Path("reports/inventory_latency_incident_report.json"),
)
NUMBERED_WORKFLOW_ARTIFACTS = (
    (Path("traces"), re.compile(r"^shopping_workflow_trace_\d+\.json$")),
    (Path("reports"), re.compile(r"^shopping_workflow_report_\d+\.json$")),
)


@dataclass(frozen=True, slots=True)
class ResetSummary:
    database_path: str
    database_removed: bool
    artifacts_root: str
    artifacts_removed: tuple[str, ...]


def _resolve(path: Path) -> Path:
    return path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def reset_demo_environment(
    database_path: Path = DEFAULT_DATABASE_PATH,
    artifacts_root: Path = DEFAULT_ARTIFACTS_ROOT,
    *,
    clear_artifacts: bool = False,
) -> ResetSummary:
    """Remove one exact SQLite file and, optionally, known generated JSON files.

    No directory or wildcard deletion is used. Product and configuration JSON files are
    intentionally outside this reset operation.
    """

    database = _resolve(Path(database_path))
    artifacts = _resolve(Path(artifacts_root))
    if database.suffix.lower() not in {".db", ".sqlite", ".sqlite3"}:
        raise ValueError("Demo database path must use .db, .sqlite, or .sqlite3")
    if database.exists() and not database.is_file():
        raise ValueError("Demo database path must identify a file")

    database_removed = database.exists()
    if database_removed:
        database.unlink()

    removed: list[str] = []
    if clear_artifacts:
        for relative_path in GENERATED_ARTIFACTS:
            target = (artifacts / relative_path).resolve()
            if target.parent != (artifacts / relative_path.parent).resolve():
                raise ValueError("Generated artifact path escaped the artifact root")
            if target.exists():
                if not target.is_file():
                    raise ValueError(f"Generated artifact is not a file: {target}")
                target.unlink()
                removed.append(relative_path.as_posix())
        for relative_directory, filename_pattern in NUMBERED_WORKFLOW_ARTIFACTS:
            directory = (artifacts / relative_directory).resolve()
            if directory.parent != artifacts:
                raise ValueError("Generated artifact directory escaped the artifact root")
            if not directory.exists():
                continue
            if not directory.is_dir():
                raise ValueError(f"Generated artifact directory is not a directory: {directory}")
            for target in directory.iterdir():
                if not filename_pattern.fullmatch(target.name):
                    continue
                if not target.is_file():
                    raise ValueError(f"Generated artifact is not a file: {target}")
                target.unlink()
                removed.append(
                    (relative_directory / target.name).as_posix()
                )

    (artifacts / "traces").mkdir(parents=True, exist_ok=True)
    (artifacts / "reports").mkdir(parents=True, exist_ok=True)
    return ResetSummary(
        database_path=str(database),
        database_removed=database_removed,
        artifacts_root=str(artifacts),
        artifacts_removed=tuple(removed),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--artifacts", type=Path, default=DEFAULT_ARTIFACTS_ROOT)
    parser.add_argument(
        "--clear-artifacts",
        action="store_true",
        help="Also remove known stage-18 JSON files, including numbered workflow runs.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_utf8_stdout()
    arguments = build_parser().parse_args(argv)
    summary = reset_demo_environment(
        arguments.database,
        arguments.artifacts,
        clear_artifacts=arguments.clear_artifacts,
    )
    print(json.dumps(asdict(summary), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
