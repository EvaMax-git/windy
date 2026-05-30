"""P4-08 Memory Relations data-access layer.

Provides CRUD for ``memory_relations`` with audit + outbox + idempotency.
State machine: ``active → resolved/cancelled``.

Constraints enforced at DB level:
* ``UNIQUE(from_memory_id, to_memory_id, relation_type)`` — duplicate rejection.
* ``CHECK(from_memory_id <> to_memory_id)`` — self-reference forbidden.
* FK → memories CASCADE on both from/to.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Session

from mneme.api.context import RequestContext
from mneme.db.audit import (
    AuditEvent,
    OutboxEvent,
    write_with_audit_outbox_idempotency,
)
from mneme.schemas.memory_relations import (
    MemoryRelationCreate,
    MemoryRelationRead,
    MemoryRelationUpdate,
)

import logging
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════
# Row mapping
# ═══════════════════════════════════════════════════════════════════════

def _relation_from_row(row: Any) -> MemoryRelationRead:
    """Map a SQL row to MemoryRelationRead, normalizing JSONB."""
    data = dict(row._mapping)
    # metadata_json may be a string from SQLite
    if isinstance(data.get("metadata_json"), str):
        try:
            data["metadata_json"] = json.loads(data["metadata_json"])
        except (json.JSONDecodeError, TypeError):
            data["metadata_json"] = {}
    return MemoryRelationRead.model_validate(data)


# ═══════════════════════════════════════════════════════════════════════
# SQL statements
# ═══════════════════════════════════════════════════════════════════════

_INSERT_RELATION = text("""
    INSERT INTO memory_relations (
      memory_relation_id, project_id,
      from_memory_id, from_memory_version,
      to_memory_id, to_memory_version,
      relation_type, relation_status,
      created_by_review_item_id, reason,
      metadata_json
    ) VALUES (
      :rid, :pid,
      :from_mid, :from_ver,
      :to_mid, :to_ver,
      :rtype, 'active',
      :review_id, :reason,
      :meta
    )
    RETURNING
      memory_relation_id, project_id,
      from_memory_id, from_memory_version,
      to_memory_id, to_memory_version,
      relation_type, relation_status,
      created_by_review_item_id, reason,
      metadata_json, created_at
""").bindparams(
    bindparam("rid", type_=PG_UUID(as_uuid=True)),
    bindparam("pid", type_=PG_UUID(as_uuid=True)),
    bindparam("from_mid", type_=PG_UUID(as_uuid=True)),
    bindparam("to_mid", type_=PG_UUID(as_uuid=True)),
    bindparam("review_id", type_=PG_UUID(as_uuid=True)),
    bindparam("meta", type_=JSONB),
)

_SELECT_RELATION = text("""
    SELECT
      memory_relation_id, project_id,
      from_memory_id, from_memory_version,
      to_memory_id, to_memory_version,
      relation_type, relation_status,
      created_by_review_item_id, reason,
      metadata_json, created_at
    FROM memory_relations
    WHERE memory_relation_id = :rid
""").bindparams(bindparam("rid", type_=PG_UUID(as_uuid=True)))

_LIST_FOR_MEMORY_COUNT = text("""
    SELECT count(*) FROM memory_relations
    WHERE from_memory_id = :mid OR to_memory_id = :mid
""").bindparams(bindparam("mid", type_=PG_UUID(as_uuid=True)))

_LIST_FOR_MEMORY = text("""
    SELECT
      memory_relation_id, project_id,
      from_memory_id, from_memory_version,
      to_memory_id, to_memory_version,
      relation_type, relation_status,
      created_by_review_item_id, reason,
      metadata_json, created_at
    FROM memory_relations
    WHERE from_memory_id = :mid OR to_memory_id = :mid
    ORDER BY created_at DESC
    LIMIT :page_size OFFSET :offset
""").bindparams(bindparam("mid", type_=PG_UUID(as_uuid=True)))

_UPDATE_RELATION = text("""
    UPDATE memory_relations
    SET relation_status = COALESCE(:status, relation_status),
        reason = COALESCE(:reason, reason),
        metadata_json = COALESCE(:meta, metadata_json)
    WHERE memory_relation_id = :rid
    RETURNING
      memory_relation_id, project_id,
      from_memory_id, from_memory_version,
      to_memory_id, to_memory_version,
      relation_type, relation_status,
      created_by_review_item_id, reason,
      metadata_json, created_at
""").bindparams(
    bindparam("rid", type_=PG_UUID(as_uuid=True)),
    bindparam("meta", type_=JSONB),
)


# ═══════════════════════════════════════════════════════════════════════
# Internal — resolve project_id from from_memory_id
# ═══════════════════════════════════════════════════════════════════════

_FETCH_MEMORY_PROJECT = text("""
    SELECT project_id FROM memories WHERE memory_id = :mid
""").bindparams(bindparam("mid", type_=PG_UUID(as_uuid=True)))


def _get_memory_project(db: Session, memory_id: UUID) -> UUID | None:
    """Return the project_id of a memory, or None if not found."""
    row = db.execute(_FETCH_MEMORY_PROJECT, {"mid": memory_id}).first()
    return row[0] if row else None


# ═══════════════════════════════════════════════════════════════════════
# Public API — create
# ═══════════════════════════════════════════════════════════════════════

def create_memory_relation(
    db: Session,
    context: RequestContext,
    *,
    payload: MemoryRelationCreate,
) -> MemoryRelationRead:
    """Create a relation between two memories.

    Enforces UNIQUE(from_memory_id, to_memory_id, relation_type) and
    CHECK(from_memory_id <> to_memory_id) at DB level.
    """
    # Validate both memories exist and fetch their current versions + project
    from mneme.db.memories import get_memory as _get_mem

    from_mem = _get_mem(db, payload.from_memory_id)
    if from_mem is None:
        raise ValueError(f"from_memory {payload.from_memory_id} not found")

    to_mem = _get_mem(db, payload.to_memory_id)
    if to_mem is None:
        raise ValueError(f"to_memory {payload.to_memory_id} not found")

    relation_id = uuid4()
    project_id = from_mem.project_id or to_mem.project_id

    outbox_event = OutboxEvent(
        event_type="memory.relation_created",
        aggregate_type="memory_relation",
        aggregate_id=relation_id,
        aggregate_version=1,
        idempotency_key=context.idempotency_key or str(uuid4()),
        producer="mneme-api",
        payload_json={
            "relation_id": str(relation_id),
            "from_memory_id": str(payload.from_memory_id),
            "to_memory_id": str(payload.to_memory_id),
            "relation_type": payload.relation_type.value,
        },
    )

    audit_event = AuditEvent(
        action="memory.relation.create",
        result="success",
        object_type="memory_relation",
        object_id=relation_id,
        project_id=project_id,
        sensitivity_level="normal",
        diff_summary={
            "from_memory_id": str(payload.from_memory_id),
            "to_memory_id": str(payload.to_memory_id),
            "relation_type": payload.relation_type.value,
        },
    )

    def _do_insert(db: Session) -> MemoryRelationRead:
        row = db.execute(
            _INSERT_RELATION,
            {
                "rid": relation_id,
                "pid": project_id,
                "from_mid": payload.from_memory_id,
                "from_ver": from_mem.current_version,
                "to_mid": payload.to_memory_id,
                "to_ver": to_mem.current_version,
                "rtype": payload.relation_type.value,
                "review_id": payload.created_by_review_item_id,
                "reason": payload.reason,
                "meta": payload.metadata_json,
            },
        ).first()
        if row is None:
            raise ValueError("failed to create memory_relation")
        return _relation_from_row(row)

    def _resolve_existing(_db: Session, _aggregate_id: UUID) -> MemoryRelationRead:
        rel = get_memory_relation(_db, _aggregate_id)
        if rel is None:
            rel = get_memory_relation(_db, relation_id)
        if rel is None:
            raise LookupError(f"memory_relation {_aggregate_id} not found during idempotent replay")
        return rel

    return write_with_audit_outbox_idempotency(
        db,
        context,
        work=_do_insert,
        audit_event=audit_event,
        outbox_event=outbox_event,
        resolve_existing=_resolve_existing,
    )


# ═══════════════════════════════════════════════════════════════════════
# Public API — read
# ═══════════════════════════════════════════════════════════════════════

def get_memory_relation(db: Session, memory_relation_id: UUID) -> MemoryRelationRead | None:
    """Look up a single relation by primary key."""
    row = db.execute(_SELECT_RELATION, {"rid": memory_relation_id}).first()
    if row is None:
        return None
    return _relation_from_row(row)


def list_memory_relations(
    db: Session,
    *,
    memory_id: UUID,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[MemoryRelationRead], int]:
    """List all relations involving *memory_id* (both from and to directions)."""
    params = {"mid": memory_id}
    total = db.execute(_LIST_FOR_MEMORY_COUNT, params).scalar_one()
    offset = (page - 1) * page_size
    rows = db.execute(
        _LIST_FOR_MEMORY,
        {**params, "page_size": page_size, "offset": offset},
    ).all()
    items = [_relation_from_row(row) for row in rows]
    return items, total


# ═══════════════════════════════════════════════════════════════════════
# Public API — update
# ═══════════════════════════════════════════════════════════════════════

def update_memory_relation(
    db: Session,
    context: RequestContext,
    *,
    memory_relation_id: UUID,
    payload: MemoryRelationUpdate,
) -> MemoryRelationRead:
    """Update mutable fields (reason, metadata_json)."""
    existing = get_memory_relation(db, memory_relation_id)
    if existing is None:
        raise ValueError(f"memory_relation {memory_relation_id} not found")

    meta_val = payload.metadata_json if payload.metadata_json is not None else None

    outbox_event = OutboxEvent(
        event_type="memory.relation_updated",
        aggregate_type="memory_relation",
        aggregate_id=memory_relation_id,
        aggregate_version=1,
        idempotency_key=f"{context.idempotency_key or ''}:update:{memory_relation_id}",
        producer="mneme-api",
        payload_json={
            "relation_id": str(memory_relation_id),
            "fields": payload.model_dump(exclude_none=True),
        },
    )

    audit_event = AuditEvent(
        action="memory.relation.update",
        result="success",
        object_type="memory_relation",
        object_id=memory_relation_id,
        project_id=existing.project_id,
        sensitivity_level="normal",
        diff_summary=payload.model_dump(exclude_none=True),
    )

    def _do_update(db: Session) -> MemoryRelationRead:
        row = db.execute(
            _UPDATE_RELATION,
            {
                "rid": memory_relation_id,
                "status": None,
                "reason": payload.reason,
                "meta": meta_val,
            },
        ).first()
        if row is None:
            raise ValueError(f"memory_relation {memory_relation_id} not found during update")
        return _relation_from_row(row)

    def _resolve_existing(_db: Session, _aggregate_id: UUID) -> MemoryRelationRead:
        rel = get_memory_relation(_db, _aggregate_id)
        if rel is None:
            raise LookupError(f"memory_relation {_aggregate_id} not found")
        return rel

    return write_with_audit_outbox_idempotency(
        db,
        context,
        work=_do_update,
        audit_event=audit_event,
        outbox_event=outbox_event,
        resolve_existing=_resolve_existing,
    )


# ═══════════════════════════════════════════════════════════════════════
# Internal — status transition helper
# ═══════════════════════════════════════════════════════════════════════

def _transition_relation_status(
    db: Session,
    context: RequestContext,
    *,
    memory_relation_id: UUID,
    to_status: str,
    tag: str,
) -> MemoryRelationRead:
    """Generic status transition for memory relations."""
    existing = get_memory_relation(db, memory_relation_id)
    if existing is None:
        raise ValueError(f"memory_relation {memory_relation_id} not found")
    if existing.relation_status != "active":
        raise ValueError(
            f"memory_relation {memory_relation_id} is '{existing.relation_status}', "
            f"expected 'active'"
        )

    outbox_event = OutboxEvent(
        event_type=f"memory.relation.{tag}",
        aggregate_type="memory_relation",
        aggregate_id=memory_relation_id,
        aggregate_version=1,
        idempotency_key=f"{context.idempotency_key or ''}:{tag}:{memory_relation_id}",
        producer="mneme-api",
        payload_json={
            "relation_id": str(memory_relation_id),
            "to_status": to_status,
        },
    )

    audit_event = AuditEvent(
        action=f"memory.relation.{tag}",
        result="success",
        object_type="memory_relation",
        object_id=memory_relation_id,
        project_id=existing.project_id,
        sensitivity_level="normal",
        diff_summary={"status": f"active→{to_status}"},
    )

    def _do_transition(db: Session) -> MemoryRelationRead:
        row = db.execute(
            _UPDATE_RELATION,
            {
                "rid": memory_relation_id,
                "status": to_status,
                "reason": None,
                "meta": None,
            },
        ).first()
        if row is None:
            raise ValueError(
                f"memory_relation {memory_relation_id} transition failed"
            )
        return _relation_from_row(row)

    def _resolve_existing(_db: Session, _aggregate_id: UUID) -> MemoryRelationRead:
        rel = get_memory_relation(_db, _aggregate_id)
        if rel is None:
            raise LookupError(f"memory_relation {_aggregate_id} not found")
        return rel

    return write_with_audit_outbox_idempotency(
        db,
        context,
        work=_do_transition,
        audit_event=audit_event,
        outbox_event=outbox_event,
        resolve_existing=_resolve_existing,
    )


# ═══════════════════════════════════════════════════════════════════════
# Public API — resolve / cancel
# ═══════════════════════════════════════════════════════════════════════

def resolve_relation(
    db: Session,
    context: RequestContext,
    *,
    memory_relation_id: UUID,
) -> MemoryRelationRead:
    """Mark a relation as resolved (active → resolved)."""
    return _transition_relation_status(
        db,
        context,
        memory_relation_id=memory_relation_id,
        to_status="resolved",
        tag="resolved",
    )


def cancel_relation(
    db: Session,
    context: RequestContext,
    *,
    memory_relation_id: UUID,
) -> MemoryRelationRead:
    """Cancel a relation (active → cancelled)."""
    return _transition_relation_status(
        db,
        context,
        memory_relation_id=memory_relation_id,
        to_status="cancelled",
        tag="cancelled",
    )
