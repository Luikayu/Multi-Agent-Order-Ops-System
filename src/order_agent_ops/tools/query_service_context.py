"""Read-only deployment and service-profile evidence query tool."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from pydantic import Field, model_validator

from order_agent_ops.config import PROJECT_ROOT
from order_agent_ops.domain.base import DomainModel, NonEmptyString
from order_agent_ops.ops.evidence_store import EvidenceDraft, EvidenceStore
from order_agent_ops.tools.schemas import EvidenceQueryResult, QueryIssue, QueryStatus


DEFAULT_PROFILES_PATH = PROJECT_ROOT / "data" / "service_profiles.json"
DEFAULT_DEPLOYMENTS_PATH = PROJECT_ROOT / "data" / "deployments.json"


class ServiceContextInclude(StrEnum):
    DEPLOYMENTS = "deployments"
    CONFIGURATION = "configuration"
    DEPENDENCIES = "dependencies"
    OWNER = "owner"
    RUNBOOK = "runbook"


class QueryServiceContextRequest(DomainModel):
    object_id: NonEmptyString
    include: list[ServiceContextInclude] = Field(min_length=1)

    @model_validator(mode="after")
    def include_must_be_unique(self) -> QueryServiceContextRequest:
        if len(set(self.include)) != len(self.include):
            raise ValueError("include must not contain duplicates")
        return self


class ServiceContextSource(Protocol):
    def query(
        self,
        include: ServiceContextInclude,
        request: QueryServiceContextRequest,
        *,
        query_ref: str,
    ) -> Sequence[EvidenceDraft]: ...


class LocalServiceContextSource:
    """Load immutable local JSON service profiles and deployment history."""

    def __init__(
        self,
        *,
        profiles_path: Path = DEFAULT_PROFILES_PATH,
        deployments_path: Path = DEFAULT_DEPLOYMENTS_PATH,
    ) -> None:
        self._profiles_path = profiles_path
        self._deployments_path = deployments_path

    @staticmethod
    def _load(path: Path, collection: str) -> tuple[dict[str, Any], list[Any]]:
        with path.open("r", encoding="utf-8") as source_file:
            payload = json.load(source_file)
        if not isinstance(payload, dict) or not isinstance(payload.get(collection), list):
            raise ValueError(f"{path} must contain a '{collection}' list")
        return payload, payload[collection]

    def query(
        self,
        include: ServiceContextInclude,
        request: QueryServiceContextRequest,
        *,
        query_ref: str,
    ) -> Sequence[EvidenceDraft]:
        if include is ServiceContextInclude.DEPLOYMENTS:
            return self._deployment_evidence(request.object_id, query_ref=query_ref)
        return self._profile_evidence(
            request.object_id,
            include,
            query_ref=query_ref,
        )

    def _deployment_evidence(
        self, object_id: str, *, query_ref: str
    ) -> list[EvidenceDraft]:
        _, deployments = self._load(self._deployments_path, "deployments")
        return [
            EvidenceDraft(
                time=deployment["deployed_at"],
                source={
                    "type": "deployment",
                    "query_ref": (
                        f"{query_ref}:deployments:{deployment['deployment_id']}"
                    ),
                },
                object={"id": object_id, "version": deployment["version"]},
                fact={
                    "observation": (
                        f"{deployment['deployment_id']} deployed version "
                        f"{deployment['version']} with status {deployment['status']}"
                    ),
                    "deployment_id": deployment["deployment_id"],
                    "status": deployment["status"],
                    "change_summary": deployment["change_summary"],
                },
            )
            for deployment in deployments
            if isinstance(deployment, dict)
            and deployment.get("object_id") == object_id
        ]

    def _profile_evidence(
        self,
        object_id: str,
        include: ServiceContextInclude,
        *,
        query_ref: str,
    ) -> list[EvidenceDraft]:
        payload, profiles = self._load(self._profiles_path, "profiles")
        profile = next(
            (
                item
                for item in profiles
                if isinstance(item, dict) and item.get("object_id") == object_id
            ),
            None,
        )
        if profile is None or include.value not in profile:
            return []
        recorded_at = profile.get("updated_at")
        if recorded_at is None:
            recorded_at = datetime.fromtimestamp(
                self._profiles_path.stat().st_mtime, timezone.utc
            )
        value = profile[include.value]
        source_type = {
            ServiceContextInclude.CONFIGURATION: "configuration",
            ServiceContextInclude.DEPENDENCIES: "dependency",
            ServiceContextInclude.OWNER: "owner",
            ServiceContextInclude.RUNBOOK: "runbook",
        }[include]
        return [
            EvidenceDraft(
                time=recorded_at,
                source={
                    "type": source_type,
                    "query_ref": f"{query_ref}:{include.value}",
                    "data_version": payload.get("data_version"),
                },
                object={"id": object_id, "version": profile.get("version")},
                fact={
                    "observation": f"Service profile {include.value}: {value}",
                    include.value: value,
                },
            )
        ]


class QueryServiceContextTool:
    """Convert service/deployment facts into persisted evidence."""

    name = "query_service_context"
    version = "v1"

    def __init__(
        self,
        source: ServiceContextSource,
        evidence_store: EvidenceStore,
    ) -> None:
        self._source = source
        self._evidence_store = evidence_store

    def execute(
        self,
        request: QueryServiceContextRequest,
        *,
        incident_id: str | None = None,
    ) -> EvidenceQueryResult:
        query_ref = f"CTX-{uuid4().hex}"
        drafts: list[EvidenceDraft] = []
        issues: list[QueryIssue] = []
        successful_sources = 0
        for include in request.include:
            try:
                results = self._source.query(
                    include,
                    request,
                    query_ref=query_ref,
                )
            except TimeoutError:
                issues.append(
                    QueryIssue(
                        source=include.value,
                        error_code="QUERY_TIMEOUT",
                        message=f"{include.value} query timed out",
                    )
                )
                continue
            except Exception as error:
                issues.append(
                    QueryIssue(
                        source=include.value,
                        error_code="DATA_SOURCE_UNAVAILABLE",
                        message=(
                            f"{include.value} source failed with "
                            f"{type(error).__name__}"
                        ),
                    )
                )
                continue

            successful_sources += 1
            if results:
                drafts.extend(results)
            else:
                issues.append(
                    QueryIssue(
                        source=include.value,
                        error_code=(
                            "OBJECT_NOT_FOUND"
                            if include is not ServiceContextInclude.DEPLOYMENTS
                            else "DEPLOYMENT_NOT_FOUND"
                        ),
                        message=(
                            f"No {include.value} data found for {request.object_id}"
                        ),
                    )
                )

        evidence = self._evidence_store.save_many(
            drafts,
            incident_id=incident_id,
        )
        if evidence and issues:
            status = QueryStatus.PARTIAL
        elif evidence:
            status = QueryStatus.SUCCESS
        elif issues and successful_sources:
            status = QueryStatus.NO_DATA
        elif issues:
            status = (
                QueryStatus.TIMEOUT
                if all(issue.error_code == "QUERY_TIMEOUT" for issue in issues)
                else QueryStatus.UNAVAILABLE
            )
        else:
            status = QueryStatus.NO_DATA
        return EvidenceQueryResult(
            status=status,
            query_ref=query_ref,
            evidence=evidence,
            issues=issues,
        )
