"""P4-06 Memory Sources data-access layer.

Sources link memories to evidence: candidates, raw_events, assets,
documents, blocks, or messages.  Each source references a specific
``memory_version`` so the FK ``(memory_id, memory_version) → memory_versions``
is satisfied.

Add / remove operations go through ``write_with_audit_outbox_idempotency``.
"""

from __future__ import annotations

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
from mneme.schemas.memory_sources import MemorySourceCreate, MemorySourceRead

# ═══════════════════════════════════════════════════════════════════════
# SQL
# ═══════════════════════════════════════════════════════════════════════

_BIND_SOURCE = [
    bindparam("sid", type_=PG_UUID(as_uuid=True)),
    bindparam("mid", type_=PG_UUID(as_uuid=True)),
    bindparam("cid", type_=PG_UUID(as_uuid=True)),
    bindparam("raw_eid", type_=PG_UUID(as_uuid=True)),
    bindparam("aid", type_=PG_UUID(as_uuid=True)),
    bindparam("did", type_=PG_UUID(as_uuid=True)),
    bindparam("bid", type_=PG_UUID(as_uuid=True)),
    bindparam("msgid", type_=PG_UUID(as_uuid=True)),
]

_INSERT_SOURCE = text("""
    INSERT INTO memory_sources (
      memory_source_id, memory_id, memory_version,
      candidate_id, raw_event_id, asset_id, document_id, block_id, message_id,
      source_span, confidence, source_role
    ) VALUES (
      :sid, :mid, :ver,
      :cid, :raw_eid, :aid, :did, :bid, :msgid,
      :span, :conf, :role
    )
    RETURNING
      memory_source_id, memory_id, memory_version,
      candidate_id, raw_event_id, asset_id, document_id, block_id, message_id,
      source_span, confidence, source_role, created_at
""").bindparams(
    *_BIND_SOURCE,
)

_DELETE_SOURCE = text("""
    DELETE FROM memory_sources
    WHERE memory_source_id = :sid
    RETURNING memory_source_id
""").bindparams(bindparam("sid", type_=PG_UUID(as_uuid=True)))

_SELECT_SOURCE = text("""
    SELECT
      memory_source_id, memory_id, memory_version,
      candidate_id, raw_event_id, asset_id, document_id, block_id, message_id,
      source_span, confidence, source_role, created_at
    FROM memory_sources
    WHERE memory_source_id = :sid
""").bindparams(bindparam("sid", type_=PG_UUID(as_uuid=True)))

_LIST_SOURCES = text("""
    SELECT
      memory_source_id, memory_id, memory_version,
      candidate_id, raw_event_id, asset_id, document_id, block_id, message_id,
      source_span, confidence, source_role, created_at
    FROM memory_sources
    WHERE memory_id = :mid
    ORDER BY memory_version DESC, created_at DESC
""").bindparams(bindparam("mid", type_=PG_UUID(as_uuid=True)))


# ═══════════════════════════════════════════════════════════════════════
# Row mapping
# ═══════════════════════════════════════════════════════════════════════

def _source_from_row(row: Any) -> MemorySourceRead:
    """Map a SQL row to MemorySourceRead, normalizing JSONB."""
    data = dict(row._mapping)
    # JSONB columns arrive as strings from SQLite; parse them into dicts.
    val = data.get("source_span")
    if isinstance(val, str):
        data["source_span"] = json.loads(val) if val else {}
    return MemorySourceRead.model_validate(data)


# ═══════════════════════════════════════════════════════════════════════
# Public API — read
# ═══════════════════════════════════════════════════════════════════════

def list_memory_sources(db: Session, *, memory_id: UUID) -> list[MemorySourceRead]:
    """List all sources for a memory, newest version first."""
    rows = db.execute(_LIST_SOURCES, {"mid": memory_id}).all()
    return [_source_from_row(row) for row in rows]


def get_memory_source(db: Session, memory_source_id: UUID) -> MemorySourceRead | None:
    """Look up a single source by ID."""
    row = db.execute(_SELECT_SOURCE, {"sid": memory_source_id}).first()
    if row is None:
        return None
    return _source_from_row(row)


# ═══════════════════════════════════════════════════════════════════════
# Public API — add
# ═══════════════════════════════════════════════════════════════════════

def add_memory_source(
    db: Session,
    context: RequestContext,
    *,
    memory_id: UUID,
    payload: MemorySourceCreate,
) -> MemorySourceRead:
    """Add a source link to a memory version.

    At least one of the source-ID fields must be non-NULL (candidate, raw_event,
    asset, document, block, or message).  The ``memory_version`` must already
    exist in ``memory_versions``.
    """
    source_id = uuid4()

    # Validate at least one source ref is provided
    refs = [
        payload.candidate_id,
        payload.raw_event_id,
        payload.asset_id,
        payload.document_id,
        payload.block_id,
        payload.message_id,
    ]
    if not any(r is not None for r in refs):
        raise ValueError("at least one source reference (candidate/raw_event/asset/document/block/message) is required")

    outbox_event = OutboxEvent(
        event_type="memory.source_added",
        aggregate_type="memory",
        aggregate_id=memory_id,
        aggregate_version=payload.memory_version,
        idempotency_key=f"{context.idempotency_key or ''}:source:{memory_id}",
        producer="mneme-api",
        payload_json={
            "memory_id": str(memory_id),
            "memory_version": payload.memory_version,
            "source_role": payload.source_role.value,
        },
    )

    audit_event = AuditEvent(
        action="memory.source_add",
        result="success",
        object_type="memory_source",
        object_id=source_id,
        sensitivity_level="normal",
        diff_summary={
            "memory_id": str(memory_id),
            "memory_version": payload.memory_version,
            "source_role": payload.source_role.value,
        },
    )

    def _do_insert(db: Session) -> MemorySourceRead:
        row = db.execute(
            _INSERT_SOURCE,
            {
                "sid": source_id,
                "mid": memory_id,
                "ver": payload.memory_version,
                "cid": payload.candidate_id,
                "raw_eid": payload.raw_event_id,
                "aid": payload.asset_id,
                "did": payload.document_id,
                "bid": payload.block_id,
                "msgid": payload.message_id,
                "span": json.dumps(payload.source_span) if isinstance(payload.source_span, dict) else payload.source_span,
                "conf": payload.confidence,
                "role": payload.source_role.value,
            },
        ).first()
        if row is None:
            raise ValueError("failed to create memory_source (FK violation or concurrent conflict)")
        return _source_from_row(row)

    def _resolve_existing(_db: Session, _aggregate_id: UUID) -> MemorySourceRead:
        s = get_memory_source(_db, source_id)
        if s is None:
            raise LookupError(f"memory_source {source_id} not found during idempotent replay")
        return s

    return write_with_audit_outbox_idempotency(
        db,
        context,
        work=_do_insert,
        audit_event=audit_event,
        outbox_event=outbox_event,
        resolve_existing=_resolve_existing,
    )


# ═══════════════════════════════════════════════════════════════════════
# Public API — remove
# ═══════════════════════════════════════════════════════════════════════

def remove_memory_source(
    db: Session,
    context: RequestContext,
    *,
    memory_source_id: UUID,
) -> bool:
    """Remove a source link (physical DELETE from ``memory_sources``).

    Returns ``True`` if the row was deleted, ``False`` if it did not exist.
    """
    existing = get_memory_source(db, memory_source_id)
    if existing is None:
        return False

    outbox_event = OutboxEvent(
        event_type="memory.source_removed",
        aggregate_type="memory",
        aggregate_id=existing.memory_id,
        aggregate_version=existing.memory_version,
        idempotency_key=f"{context.idempotency_key or ''}:source_remove:{memory_source_id}",
        producer="mneme-api",
        payload_json={
            "memory_source_id": str(memory_source_id),
            "memory_id": str(existing.memory_id),
        },
    )

    audit_event = AuditEvent(
        action="memory.source_remove",
        result="success",
        object_type="memory_source",
        object_id=memory_source_id,
        sensitivity_level="normal",
        diff_summary={
            "memory_id": str(existing.memory_id),
            "source_role": existing.source_role,
        },
    )

    def _do_delete(db: Session) -> bool:
        row = db.execute(_DELETE_SOURCE, {"sid": memory_source_id}).first()
        return row is not None

    def _resolve_existing(_db: Session, _aggregate_id: UUID) -> bool:
        # Idempotent: if we got here, the source was already removed
        return False

    return write_with_audit_outbox_idempotency(
        db,
        context,
        work=_do_delete,
        audit_event=audit_event,
        outbox_event=outbox_event,
        resolve_existing=_resolve_existing,
    )
