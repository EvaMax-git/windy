"""P4-04 Memory Candidates data-access layer.

Provides CRUD against ``memory_candidates`` with SHA-256 candidate_hash dedup.
Every write is wrapped in ``write_with_audit_outbox_idempotency``.

Dedup strategy
--------------
``candidate_hash`` = SHA-256(title + candidate_text + source_type + source_id)
The ``UNIQUE(project_id, candidate_hash)`` constraint enforces dedup per project.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session

from mneme.api.context import RequestContext
from mneme.db.audit import (
    AuditEvent,
    OutboxEvent,
    write_with_audit_outbox_idempotency,
)
from mneme.schemas.memory_candidates import (
    MemoryCandidateCreate,
    MemoryCandidateRead,
    MemoryCandidateUpdate,
)


# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════

def compute_candidate_hash(
    *,
    title: str | None,
    candidate_text: str,
    source_type: str,
    source_id: UUID | None,
) -> str:
    """Compute SHA-256 hash for candidate dedup.

    Formula: SHA-256(title + candidate_text + source_type + source_id)
    Matches GAP field #16 specification.
    """
    raw = f"{title or ''}|{candidate_text}|{source_type}|{source_id or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ═══════════════════════════════════════════════════════════════════
# SQL — memory_candidates
# ═══════════════════════════════════════════════════════════════════

_INSERT_CANDIDATE = text("""
    INSERT INTO memory_candidates (
      candidate_id,
      project_id,
      source_type,
      source_id,
      submitted_by_actor_type,
      submitted_by_actor_id,
      title,
      candidate_text,
      candidate_hash,
      sensitivity_level,
      confidence_score,
      review_required,
      metadata_json
    )
    VALUES (
      :candidate_id,
      :project_id,
      :source_type,
      :source_id,
      :submitted_by_actor_type,
      :submitted_by_actor_id,
      :title,
      :candidate_text,
      :candidate_hash,
      :sensitivity_level,
      :confidence_score,
      :review_required,
      :metadata_json
    )
    ON CONFLICT (project_id, candidate_hash) DO NOTHING
    RETURNING
      candidate_id, project_id,
      source_type, source_id,
      submitted_by_actor_type, submitted_by_actor_id,
      title, candidate_text, candidate_hash,
      sensitivity_level, candidate_status,
      confidence_score, review_required,
      metadata_json,
      created_at, updated_at
""").bindparams(
    bindparam("candidate_id", type_=PG_UUID(as_uuid=True)),
    bindparam("project_id", type_=PG_UUID(as_uuid=True)),
    bindparam("source_id", type_=PG_UUID(as_uuid=True)),
    bindparam("submitted_by_actor_id", type_=PG_UUID(as_uuid=True)),
)

_SELECT_CANDIDATE_BY_ID = text("""
    SELECT
      candidate_id, project_id,
      source_type, source_id,
      submitted_by_actor_type, submitted_by_actor_id,
      title, candidate_text, candidate_hash,
      sensitivity_level, candidate_status,
      confidence_score, review_required,
      metadata_json,
      created_at, updated_at
    FROM memory_candidates
    WHERE candidate_id = :candidate_id
""").bindparams(bindparam("candidate_id", type_=PG_UUID(as_uuid=True)))

_SELECT_CANDIDATE_BY_HASH = text("""
    SELECT
      candidate_id, project_id,
      source_type, source_id,
      submitted_by_actor_type, submitted_by_actor_id,
      title, candidate_text, candidate_hash,
      sensitivity_level, candidate_status,
      confidence_score, review_required,
      metadata_json,
      created_at, updated_at
    FROM memory_candidates
    WHERE project_id = :project_id
      AND candidate_hash = :candidate_hash
""").bindparams(bindparam("project_id", type_=PG_UUID(as_uuid=True)))

_LIST_COUNT = text("""
    SELECT count(*) FROM memory_candidates
    WHERE (:project_id IS NULL OR project_id = :project_id)
      AND (:source_type IS NULL OR source_type = :source_type)
      AND (:candidate_status IS NULL OR candidate_status = :candidate_status)
      AND (:created_after IS NULL OR created_at >= :created_after)
      AND (:created_before IS NULL OR created_at <= :created_before)
""").bindparams(bindparam("project_id", type_=PG_UUID(as_uuid=True)))

_LIST_QUERY = text("""
    SELECT
      candidate_id, project_id,
      source_type, source_id,
      submitted_by_actor_type, submitted_by_actor_id,
      title, candidate_text, candidate_hash,
      sensitivity_level, candidate_status,
      confidence_score, review_required,
      metadata_json,
      created_at, updated_at
    FROM memory_candidates
    WHERE (:project_id IS NULL OR project_id = :project_id)
      AND (:source_type IS NULL OR source_type = :source_type)
      AND (:candidate_status IS NULL OR candidate_status = :candidate_status)
      AND (:created_after IS NULL OR created_at >= :created_after)
      AND (:created_before IS NULL OR created_at <= :created_before)
    ORDER BY created_at DESC
    LIMIT :page_size OFFSET :offset
""").bindparams(bindparam("project_id", type_=PG_UUID(as_uuid=True)))

_UPDATE_CANDIDATE = text("""
    UPDATE memory_candidates
    SET title = COALESCE(:title, title),
        candidate_text = COALESCE(:candidate_text, candidate_text),
        sensitivity_level = COALESCE(:sensitivity_level, sensitivity_level),
        confidence_score = COALESCE(:confidence_score, confidence_score),
        metadata_json = COALESCE(:metadata_json, metadata_json),
        updated_at = now()
    WHERE candidate_id = :candidate_id
    RETURNING
      candidate_id, project_id,
      source_type, source_id,
      submitted_by_actor_type, submitted_by_actor_id,
      title, candidate_text, candidate_hash,
      sensitivity_level, candidate_status,
      confidence_score, review_required,
      metadata_json,
      created_at, updated_at
""").bindparams(bindparam("candidate_id", type_=PG_UUID(as_uuid=True)))

_UPDATE_CANDIDATE_STATUS = text("""
    UPDATE memory_candidates
    SET candidate_status = :candidate_status,
        updated_at = now()
    WHERE candidate_id = :candidate_id
      AND candidate_status = :from_status
    RETURNING
      candidate_id, project_id,
      source_type, source_id,
      submitted_by_actor_type, submitted_by_actor_id,
      title, candidate_text, candidate_hash,
      sensitivity_level, candidate_status,
      confidence_score, review_required,
      metadata_json,
      created_at, updated_at
""").bindparams(bindparam("candidate_id", type_=PG_UUID(as_uuid=True)))

_DELETE_CANDIDATE = text("""
    DELETE FROM memory_candidates
    WHERE candidate_id = :candidate_id
    RETURNING
      candidate_id, project_id,
      source_type, source_id,
      submitted_by_actor_type, submitted_by_actor_id,
      title, candidate_text, candidate_hash,
      sensitivity_level, candidate_status,
      confidence_score, review_required,
      metadata_json,
      created_at, updated_at
""").bindparams(bindparam("candidate_id", type_=PG_UUID(as_uuid=True)))


# ═══════════════════════════════════════════════════════════════════
# Row mapping
# ═══════════════════════════════════════════════════════════════════

def _candidate_from_row(row: Any) -> MemoryCandidateRead:
    data = dict(row._mapping)
    # Parse JSONB fields that may arrive as strings from SQLite
    if "metadata_json" in data and isinstance(data["metadata_json"], str):
        try:
            data["metadata_json"] = json.loads(data["metadata_json"])
        except (json.JSONDecodeError, TypeError):
            data["metadata_json"] = {}
    return MemoryCandidateRead.model_validate(data)


def _idempotent_resolve(db: Session, candidate_id: UUID) -> MemoryCandidateRead:
    row = db.execute(
        _SELECT_CANDIDATE_BY_ID, {"candidate_id": candidate_id}
    ).first()
    if row is None:
        raise LookupError(f"candidate {candidate_id} not found during idempotent replay")
    return _candidate_from_row(row)


# ═══════════════════════════════════════════════════════════════════
# Public API — submit
# ═══════════════════════════════════════════════════════════════════

def submit_candidate(
    db: Session,
    context: RequestContext,
    *,
    payload: MemoryCandidateCreate,
) -> MemoryCandidateRead:
    """Submit a new memory candidate with SHA-256 hash dedup (idempotent).

    Uses ``ON CONFLICT (project_id, candidate_hash) DO NOTHING RETURNING``
    to make duplicate submissions safe: the same content submitted twice
    (even with a different idempotency key) returns the existing candidate
    instead of a 409 conflict.

    candidate_hash = SHA-256(title + candidate_text + source_type + source_id)
    """
    candidate_id = uuid4()
    candidate_hash = compute_candidate_hash(
        title=payload.title,
        candidate_text=payload.candidate_text,
        source_type=payload.source_type.value,
        source_id=payload.source_id,
    )

    # Check for existing candidate by content hash first (fast path for dedup).
    # This also handles the case where ON CONFLICT DO NOTHING skips the INSERT
    # and returns no row.
    if payload.project_id:
        existing = get_candidate_by_hash(
            db, project_id=payload.project_id, candidate_hash=candidate_hash,
        )
        if existing is not None:
            return existing

    outbox_event = OutboxEvent(
        event_type="memory_candidate.submitted",
        aggregate_type="memory_candidate",
        aggregate_id=candidate_id,
        aggregate_version=1,
        idempotency_key=context.idempotency_key or str(uuid4()),
        producer="mneme-api",
        payload_json={
            "project_id": str(payload.project_id) if payload.project_id else None,
            "source_type": payload.source_type.value,
            "source_id": str(payload.source_id) if payload.source_id else None,
            "candidate_hash": candidate_hash,
        },
    )

    audit_event = AuditEvent(
        action="memory_candidate.submit",
        result="success",
        object_type="memory_candidate",
        object_id=candidate_id,
        project_id=payload.project_id,
        sensitivity_level=payload.sensitivity_level,
    )

    def _do_insert(db: Session) -> MemoryCandidateRead:
        row = db.execute(
            _INSERT_CANDIDATE,
            {
                "candidate_id": candidate_id,
                "project_id": payload.project_id,
                "source_type": payload.source_type.value,
                "source_id": payload.source_id,
                "submitted_by_actor_type": context.actor.actor_type,
                "submitted_by_actor_id": context.actor.actor_id,
                "title": payload.title,
                "candidate_text": payload.candidate_text,
                "candidate_hash": candidate_hash,
                "sensitivity_level": payload.sensitivity_level,
                "confidence_score": payload.confidence_score,
                "review_required": payload.review_required,
                "metadata_json": json.dumps(payload.metadata_json),
            },
        ).first()  # .first() returns None on ON CONFLICT DO NOTHING
        if row is None:
            raise ValueError(
                f"candidate insert skipped by ON CONFLICT (hash={candidate_hash[:16]})"
            )
        return _candidate_from_row(row)

    try:
        return write_with_audit_outbox_idempotency(
            db,
            context,
            work=_do_insert,
            audit_event=audit_event,
            outbox_event=outbox_event,
            resolve_existing=_idempotent_resolve,
        )
    except ValueError:
        # ON CONFLICT DO NOTHING skipped the INSERT — resolve to existing candidate.
        # This can happen when a concurrent request inserted the same hash between
        # our pre-check and INSERT, or on backends where the pre-check misses
        # (e.g. SQLite with non-standard UNIQUE enforcement).
        existing = get_candidate_by_hash(
            db, project_id=payload.project_id, candidate_hash=candidate_hash,
        )
        if existing is not None:
            return existing
        raise


# ═══════════════════════════════════════════════════════════════════
# Public API — read
# ═══════════════════════════════════════════════════════════════════

def get_candidate_by_id(db: Session, candidate_id: UUID) -> MemoryCandidateRead | None:
    """Look up a memory candidate by primary key."""
    row = db.execute(
        _SELECT_CANDIDATE_BY_ID, {"candidate_id": candidate_id}
    ).first()
    if row is None:
        return None
    return _candidate_from_row(row)


def get_candidate_by_hash(
    db: Session,
    *,
    project_id: UUID,
    candidate_hash: str,
) -> MemoryCandidateRead | None:
    """Look up a memory candidate by project_id + candidate_hash (dedup check)."""
    row = db.execute(
        _SELECT_CANDIDATE_BY_HASH,
        {"project_id": project_id, "candidate_hash": candidate_hash},
    ).first()
    if row is None:
        return None
    return _candidate_from_row(row)


def list_candidates(
    db: Session,
    *,
    project_id: UUID | None = None,
    source_type: str | None = None,
    candidate_status: str | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[MemoryCandidateRead], int]:
    """List memory candidates with filters and pagination."""
    params = {
        "project_id": project_id,
        "source_type": source_type,
        "candidate_status": candidate_status,
        "created_after": created_after,
        "created_before": created_before,
    }
    total = db.execute(_LIST_COUNT, params).scalar_one()
    offset = (page - 1) * page_size
    rows = db.execute(
        _LIST_QUERY,
        {**params, "page_size": page_size, "offset": offset},
    ).all()
    items = [_candidate_from_row(row) for row in rows]
    return items, total


# ═══════════════════════════════════════════════════════════════════
# Public API — update
# ═══════════════════════════════════════════════════════════════════

def update_candidate(
    db: Session,
    context: RequestContext,
    *,
    candidate_id: UUID,
    payload: MemoryCandidateUpdate,
) -> MemoryCandidateRead:
    """Update mutable fields of a memory candidate."""
    existing = get_candidate_by_id(db, candidate_id)
    if existing is None:
        raise ValueError(f"candidate {candidate_id} not found")

    outbox_event = OutboxEvent(
        event_type="memory_candidate.updated",
        aggregate_type="memory_candidate",
        aggregate_id=candidate_id,
        aggregate_version=1,
        idempotency_key=f"{context.idempotency_key or ''}:update:{candidate_id}",
        producer="mneme-api",
        payload_json={
            "candidate_id": str(candidate_id),
            "fields": payload.model_dump(exclude_none=True),
        },
    )

    audit_event = AuditEvent(
        action="memory_candidate.update",
        result="success",
        object_type="memory_candidate",
        object_id=candidate_id,
        project_id=existing.project_id,
        sensitivity_level=existing.sensitivity_level,
        diff_summary=payload.model_dump(exclude_none=True),
    )

    def _do_update(db: Session) -> MemoryCandidateRead:
        row = db.execute(
            _UPDATE_CANDIDATE,
            {
                "candidate_id": candidate_id,
                "title": payload.title,
                "candidate_text": payload.candidate_text,
                "sensitivity_level": payload.sensitivity_level,
                "confidence_score": payload.confidence_score,
                "metadata_json": json.dumps(payload.metadata_json) if payload.metadata_json is not None else None,
            },
        ).one()
        return _candidate_from_row(row)

    def _resolve_existing(_db: Session, _aggregate_id: UUID) -> MemoryCandidateRead:
        c = get_candidate_by_id(_db, candidate_id)
        if c is None:
            raise LookupError(f"candidate {candidate_id} not found")
        return c

    return write_with_audit_outbox_idempotency(
        db,
        context,
        work=_do_update,
        audit_event=audit_event,
        outbox_event=outbox_event,
        resolve_existing=_resolve_existing,
    )


# ═══════════════════════════════════════════════════════════════════
# Public API — status transitions
# ═══════════════════════════════════════════════════════════════════

def update_candidate_status(
    db: Session,
    *,
    candidate_id: UUID,
    from_status: str,
    to_status: str,
) -> MemoryCandidateRead | None:
    """Transition a candidate between statuses atomically.

    Returns the updated row, or ``None`` if the candidate was not in *from_status*.
    """
    row = db.execute(
        _UPDATE_CANDIDATE_STATUS,
        {
            "candidate_id": candidate_id,
            "from_status": from_status,
            "candidate_status": to_status,
        },
    ).first()
    if row is None:
        return None
    return _candidate_from_row(row)


# ═══════════════════════════════════════════════════════════════════
# Public API — delete
# ═══════════════════════════════════════════════════════════════════

def delete_candidate(
    db: Session,
    context: RequestContext,
    *,
    candidate_id: UUID,
) -> MemoryCandidateRead | None:
    """Delete a memory candidate by primary key (hard delete)."""
    existing = get_candidate_by_id(db, candidate_id)
    if existing is None:
        return None

    outbox_event = OutboxEvent(
        event_type="memory_candidate.deleted",
        aggregate_type="memory_candidate",
        aggregate_id=candidate_id,
        aggregate_version=1,
        idempotency_key=f"{context.idempotency_key or ''}:delete:{candidate_id}",
        producer="mneme-api",
        payload_json={
            "candidate_id": str(candidate_id),
            "source_type": existing.source_type,
        },
    )

    audit_event = AuditEvent(
        action="memory_candidate.delete",
        result="success",
        object_type="memory_candidate",
        object_id=candidate_id,
        project_id=existing.project_id,
        sensitivity_level=existing.sensitivity_level,
        diff_summary={
            "source_type": existing.source_type,
            "candidate_status": existing.candidate_status,
        },
    )

    def _do_delete(db: Session) -> MemoryCandidateRead:
        row = db.execute(
            _DELETE_CANDIDATE, {"candidate_id": candidate_id}
        ).first()
        if row is None:
            raise ValueError(f"candidate {candidate_id} could not be deleted")
        return _candidate_from_row(row)

    def _resolve_existing(_db: Session, _aggregate_id: UUID) -> MemoryCandidateRead:
        return existing

    return write_with_audit_outbox_idempotency(
        db,
        context,
        work=_do_delete,
        audit_event=audit_event,
        outbox_event=outbox_event,
        resolve_existing=_resolve_existing,
    )
