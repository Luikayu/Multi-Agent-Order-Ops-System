"""Deterministic validation of diagnosis-to-evidence references."""

from __future__ import annotations

from collections.abc import Iterable, Set

from order_agent_ops.domain.incidents import DiagnosisResult
from order_agent_ops.ops.evidence_store import EvidenceStore


class EvidenceValidationError(ValueError):
    """Base error for a diagnosis that cannot be supported by stored evidence."""


class UnknownEvidenceReferenceError(EvidenceValidationError):
    """Raised when a diagnosis cites evidence outside the current incident."""


class InsufficientEvidenceError(EvidenceValidationError):
    """Raised when a confident root cause lacks required independent sources."""


class EvidenceValidator:
    """Reject fabricated IDs and unsupported high-confidence conclusions."""

    def __init__(self, evidence_store: EvidenceStore) -> None:
        self._evidence_store = evidence_store

    def validate(
        self,
        diagnosis: DiagnosisResult,
        *,
        incident_id: str,
        required_source_types: Set[str] = frozenset(),
    ) -> DiagnosisResult:
        if diagnosis.incident_id != incident_id:
            raise EvidenceValidationError(
                "Diagnosis incident_id does not match the investigated incident"
            )

        available = {
            evidence.evidence_id: evidence
            for evidence in self._evidence_store.query(incident_id=incident_id)
        }
        referenced_ids = set(self._all_references(diagnosis))
        unknown = sorted(referenced_ids - available.keys())
        if unknown:
            raise UnknownEvidenceReferenceError(
                "Diagnosis references evidence not stored for this incident: "
                + ", ".join(unknown)
            )

        if not diagnosis.root_cause_candidates and not diagnosis.missing_information:
            raise InsufficientEvidenceError(
                "A diagnosis without root-cause candidates must declare missing information"
            )

        for candidate in diagnosis.root_cause_candidates:
            if candidate.confidence <= 0.5 or not required_source_types:
                continue
            source_types = {
                available[evidence_id].source.type
                for evidence_id in candidate.evidence_ids
            }
            missing_types = sorted(required_source_types - source_types)
            if missing_types:
                raise InsufficientEvidenceError(
                    "High-confidence root cause is missing required evidence types: "
                    + ", ".join(missing_types)
                )
        return diagnosis

    @staticmethod
    def _all_references(diagnosis: DiagnosisResult) -> Iterable[str]:
        for fact in diagnosis.facts:
            yield from fact.evidence_ids
        for candidate in diagnosis.root_cause_candidates:
            yield from candidate.evidence_ids
        for action in diagnosis.recommended_actions:
            yield from action.evidence_ids
