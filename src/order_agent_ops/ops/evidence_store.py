"""Platform-owned evidence creation, persistence, linkage, and querying."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from pydantic import AwareDatetime

from order_agent_ops.domain.base import DomainModel
from order_agent_ops.domain.evidence import (
    EvidenceFact,
    EvidenceObject,
    EvidenceRecord,
    EvidenceSource,
    EvidenceTimeWindow,
)
from order_agent_ops.storage.database import Database
from order_agent_ops.storage.repositories import (
    EvidenceRepository,
    IncidentRepository,
)


class EvidenceDraft(DomainModel):
    """Evidence contents before the platform assigns an evidence_id."""

    time: AwareDatetime | EvidenceTimeWindow
    source: EvidenceSource
    object: EvidenceObject
    fact: EvidenceFact


class EvidenceStore:
    """Store immutable five-field evidence and optional incident links."""

    def __init__(self, database: Database) -> None:
        self._database = database
        self._evidence = EvidenceRepository(database)
        self._incidents = IncidentRepository(database)

    def save(
        self,
        draft: EvidenceDraft,
        *,
        incident_id: str | None = None,
    ) -> EvidenceRecord:
        if incident_id is not None and self._incidents.get(incident_id) is None:
            raise ValueError(f"Incident not found: {incident_id}")

        record = EvidenceRecord(
            evidence_id=f"E-{uuid4().hex}",
            **draft.model_dump(),
        )
        self._evidence.create(record)
        if incident_id is not None:
            with self._database.connection() as connection:
                connection.execute(
                    """
                    INSERT INTO incident_evidence (incident_id, evidence_id, linked_at)
                    VALUES (?, ?, ?)
                    """,
                    (incident_id, record.evidence_id, datetime.now(timezone.utc).isoformat()),
                )
        return record

    def save_many(
        self,
        drafts: list[EvidenceDraft],
        *,
        incident_id: str | None = None,
    ) -> list[EvidenceRecord]:
        return [self.save(draft, incident_id=incident_id) for draft in drafts]

    def get(self, evidence_id: str) -> EvidenceRecord | None:
        return self._evidence.get(evidence_id)

    def query(
        self,
        *,
        incident_id: str | None = None,
        object_id: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> list[EvidenceRecord]:
        linked_ids: set[str] | None = None
        if incident_id is not None:
            with self._database.connection() as connection:
                rows = connection.execute(
                    """
                    SELECT evidence_id FROM incident_evidence
                    WHERE incident_id = ? ORDER BY linked_at, evidence_id
                    """,
                    (incident_id,),
                ).fetchall()
            linked_ids = {row["evidence_id"] for row in rows}

        records = self._evidence.list_all()
        return [
            record
            for record in records
            if (linked_ids is None or record.evidence_id in linked_ids)
            and (object_id is None or record.object.id == object_id)
            and self._matches_time(record, start_time=start_time, end_time=end_time)
        ]

    @staticmethod
    def _matches_time(
        record: EvidenceRecord,
        *,
        start_time: datetime | None,
        end_time: datetime | None,
    ) -> bool:
        if isinstance(record.time, EvidenceTimeWindow):
            record_start = record.time.start
            record_end = record.time.end
        else:
            record_start = record.time
            record_end = record.time
        if start_time is not None and record_end < start_time:
            return False
        if end_time is not None and record_start > end_time:
            return False
        return True
