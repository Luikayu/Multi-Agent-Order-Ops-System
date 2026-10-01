"""Shared helpers for repeatable stage-18 demonstration scripts."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any, Protocol

from order_agent_ops.config import PROJECT_ROOT


DEFAULT_ARTIFACTS_ROOT = PROJECT_ROOT / "artifacts"
AGENT_COMPONENTS = {
    "shopping-assistant-agent",
    "order-coordinator-agent",
    "inventory-agent",
    "risk-agent",
    "ops-guardian-agent",
}


class JsonResponse(Protocol):
    status_code: int
    text: str

    def json(self) -> Any: ...


class ApiClient(Protocol):
    def request(self, method: str, url: str, **kwargs: Any) -> JsonResponse: ...


class DemoStageError(RuntimeError):
    """A public script error that identifies the failed demonstration stage."""

    def __init__(self, stage: str, message: str) -> None:
        self.stage = stage
        super().__init__(f"[{stage}] {message}")


def configure_utf8_stdout() -> None:
    """Keep Chinese demo output readable when PowerShell uses a legacy code page."""

    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8")


def request_json(
    client: ApiClient,
    method: str,
    path: str,
    *,
    stage: str,
    expected_status: int = 200,
    **kwargs: Any,
) -> Any:
    """Call one API stage and raise a stage-labelled error on any failure."""

    try:
        response = client.request(method, path, **kwargs)
    except Exception as error:  # pragma: no cover - exercised by real CLI failures
        raise DemoStageError(stage, f"API request failed: {type(error).__name__}") from error
    if response.status_code != expected_status:
        try:
            detail = response.json()
        except Exception:
            detail = response.text[:300]
        raise DemoStageError(
            stage,
            f"expected HTTP {expected_status}, got {response.status_code}: {detail}",
        )
    try:
        return response.json()
    except Exception as error:
        raise DemoStageError(stage, "API returned invalid JSON") from error


def write_json_artifact(path: Path, payload: Any) -> Path:
    """Write deterministic UTF-8 JSON, creating only the requested parent folder."""

    resolved = Path(path).resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    temporary = resolved.with_suffix(f"{resolved.suffix}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(resolved)
    return resolved


def agent_execution_order(spans: list[dict[str, Any]]) -> list[str]:
    """Return first-seen Agent components while preserving trace start order."""

    order: list[str] = []
    for span in spans:
        component = span.get("component")
        if component in AGENT_COMPONENTS and component not in order:
            order.append(component)
    return order


def relative_artifact_path(path: Path) -> str:
    """Prefer a portable project-relative path in generated reports."""

    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return resolved.name
