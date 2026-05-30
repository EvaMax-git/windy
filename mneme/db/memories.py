"""P4-05 Memories data-access layer.

Provides CRUD against ``memories`` with canonical_key generation, version
recording, and state-machine enforcement.  Every write is wrapped in
``write_with_audit_outbox_idempotency``.

Canonical key
-------------
Format: ``{project_code}-mem-{increment}``.  Generated from the project's
``project_code`` + the next sequential number for that project.  A custom
key can be supplied for manual creation; it must be unique per project.

Generation uses ``pg_advisory_xact_lock(project_id_hash)`` to serialize
per-project, preventing concurrent inserts from assigning the same key.

State machine
-------------
::

    draft → active  (via review_item approval)
    draft → deleted
    active → expired (manual expire)
    active → merged  (merged into another memory)
    active → deleted
    expired → active (restore)
    expired → deleted
    deleted → active (restore)

All transitions are guarded by ``WHERE status = :from_status`` to prevent
lost updates from concurrent requests.

Hard constraint: ``status='active'`` ⇒ ``activated_by_review_item_id IS NOT NULL``.

Merge semantics
---------------
``POST /memory/{memory_id}/merge`` with body ``{target_memory_id}``:

* *memory_id* (the URL path parameter) is the **survivor** — it absorbs
  the consumed memory's text and remains active.
* *target_memory_id* (the request body) is **consumed** — its text is
  appended to the survivor, its status becomes ``'merged'``, and a
  ``merged_into`` relation is created: consumed → survivor.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from mneme.db.compat import PortableUUID

from mneme.api.context import RequestContext
from mneme.db.audit import (
    AuditEvent,
    OutboxEvent,
    write_with_audit_outbox_idempotency,
)
from mneme.schemas.memories import (
    MemoryCreate,
    MemoryMerge,
    MemoryRead,
    MemoryUpdate,
)

import logging
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════════

_fetch_project_code = text("""
    SELECT project_code FROM projects WHERE project_id = :pid
""").bindparams(bindparam("pid", type_=PortableUUID(as_uuid=True)))

_next_mem_num = text("""
    SELECT count(*) + 1 FROM memories WHERE project_id = :pid
""").bindparams(bindparam("pid", type_=PortableUUID(as_uuid=True)))

# Advisory lock key derivation — maps a UUID project_id to an int64 lock key
# so pg_advisory_xact_lock can serialize canonical_key generation per project.
_ADVISORY_LOCK = text("SELECT pg_advisory_xact_lock(:key)")


def _project_lock_key(project_id: UUID) -> int:
    """Derive a deterministic int64 lock key from a UUID project_id."""
    pi = project_id.int
    return (pi >> 64) ^ (pi & 0xFFFFFFFFFFFFFFFF)


def _generate_canonical_key(db: Session, project_id: UUID) -> str:
    """Generate canonical_key = ``{project_code}-mem-{next_num}``.

    On PostgreSQL, grabs a per-project advisory lock so concurrent inserts
    within the same project are serialised, preventing duplicate key
    assignment.  The lock is automatically released at transaction end.

    On SQLite the lock is a no-op (SQLite serialises writes anyway).
    """
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        db.execute(_ADVISORY_LOCK, {"key": _project_lock_key(project_id)})

    row = db.execute(_fetch_project_code, {"pid": project_id}).first()
    if row is None:
        raise ValueError(f"project {project_id} not found")
    project_code = row[0]
    next_num = db.execute(_next_mem_num, {"pid": project_id}).scalar_one()
    return f"{project_code}-mem-{next_num}"


def _has_store_id_column(db: Session) -> bool:
    """Check whether the ``memories`` table has a ``store_id`` column.

    PostgreSQL always has it.  SQLite may not depending on migration state.
    """
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        return True
    # SQLite — check via PRAGMA
    try:
        rows = db.execute(text("PRAGMA table_info('memories')")).all()
        return "store_id" in {r[1] for r in rows}
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════════
# SQL — memory_versions (internal)
# ═══════════════════════════════════════════════════════════════════════

_INSERT_VERSION = text("""
    INSERT INTO memory_versions (
      memory_version_id, memory_id, version, action,
      before_json, after_json,
      actor_type, actor_id,
      review_item_id,
      reason
    ) VALUES (
      :vid, :mid, :ver, :action,
      :before_json, :after_json,
      :actor_type, :actor_id,
      :review_item_id,
      :reason
    )
""").bindparams(
    bindparam("vid", type_=PortableUUID(as_uuid=True)),
    bindparam("mid", type_=PortableUUID(as_uuid=True)),
    bindparam("actor_id", type_=PortableUUID(as_uuid=True)),
    bindparam("review_item_id", type_=PortableUUID(as_uuid=True)),
)


def _record_memory_version(
    db: Session,
    *,
    memory_id: UUID,
    version: int,
    action: str,
    before_json: dict[str, Any],
    after_json: dict[str, Any],
    actor_type: str,
    actor_id: UUID | None,
    review_item_id: UUID | None = None,
    reason: str | None = None,
) -> None:
    """Insert a row into ``memory_versions``."""
    db.execute(
        _INSERT_VERSION,
        {
            "vid": uuid4(),
            "mid": memory_id,
            "ver": version,
            "action": action,
            "before_json": json.dumps(before_json),
            "after_json": json.dumps(after_json),
            "actor_type": actor_type,
            "actor_id": actor_id,
            "review_item_id": review_item_id,
            "reason": reason,
        },
    )


# ═══════════════════════════════════════════════════════════════════════
# SQL — memory_sources (internal, for activate)
# ═══════════════════════════════════════════════════════════════════════

_INSERT_SOURCE = text("""
    INSERT INTO memory_sources (
      memory_source_id, memory_id, memory_version,
      candidate_id,
      source_role
    ) VALUES (
      :sid, :mid, :ver,
      :cid,
      'origin'
    )
""").bindparams(
    bindparam("sid", type_=PortableUUID(as_uuid=True)),
    bindparam("mid", type_=PortableUUID(as_uuid=True)),
    bindparam("cid", type_=PortableUUID(as_uuid=True)),
)


def _record_memory_source(
    db: Session,
    *,
    memory_id: UUID,
    version: int,
    candidate_id: UUID,
) -> None:
    """Insert a row into ``memory_sources`` linking to the origin candidate."""
    db.execute(
        _INSERT_SOURCE,
        {
            "sid": uuid4(),
            "mid": memory_id,
            "ver": version,
            "cid": candidate_id,
        },
    )


# ═══════════════════════════════════════════════════════════════════════
# SQL — memory_relations (internal, for merge)
# ═══════════════════════════════════════════════════════════════════════

_INSERT_RELATION = text("""
    INSERT INTO memory_relations (
      memory_relation_id, project_id,
      from_memory_id, from_memory_version,
      to_memory_id, to_memory_version,
      relation_type, reason
    ) VALUES (
      :rid, :pid,
      :from_mid, :from_ver,
      :to_mid, :to_ver,
      'merged_into', :reason
    )
    ON CONFLICT (from_memory_id, to_memory_id, relation_type) DO NOTHING
""").bindparams(
    bindparam("rid", type_=PortableUUID(as_uuid=True)),
    bindparam("pid", type_=PortableUUID(as_uuid=True)),
    bindparam("from_mid", type_=PortableUUID(as_uuid=True)),
    bindparam("to_mid", type_=PortableUUID(as_uuid=True)),
)


# ═══════════════════════════════════════════════════════════════════════
# SQL — memories CRUD
# ═══════════════════════════════════════════════════════════════════════

_INSERT_MEMORY = text("""
    INSERT INTO memories (
      memory_id, project_id, canonical_key,
      title, memory_text,
      store_id,
      current_version, sensitivity_level, status,
      node_type,
      activated_from_candidate_id, activated_by_review_item_id, activated_at
    ) VALUES (
      :mid, :pid, :ckey,
      :title, :mtext,
      :store_id,
      :ver, :slevel, :status,
      :node_type,
      :afcid, :arid, :aat
    )
    RETURNING
      memory_id, project_id, store_id, canonical_key,
      title, memory_text, current_version,
      sensitivity_level, status,
      node_type,
      activated_from_candidate_id, activated_by_review_item_id, activated_at,
      expired_at,
      quality_score, search_weight, last_refined_at,
      decay_score, decay_state, last_decayed_at, last_reinforced_at,
      emotion_charge, uncertainty_score, last_emotion_inferred_at,
      created_at, updated_at
""").bindparams(
    bindparam("mid", type_=PortableUUID(as_uuid=True)),
    bindparam("pid", type_=PortableUUID(as_uuid=True)),
    bindparam("store_id", type_=PortableUUID(as_uuid=True)),
    bindparam("afcid", type_=PortableUUID(as_uuid=True)),
    bindparam("arid", type_=PortableUUID(as_uuid=True)),
)

_SELECT_MEMORY = text("""
    SELECT
      memory_id, project_id, store_id, canonical_key,
      title, memory_text, current_version,
      sensitivity_level, status,
      node_type,
      activated_from_candidate_id, activated_by_review_item_id, activated_at,
      expired_at,
      quality_score, search_weight, last_refined_at,
      decay_score, decay_state, last_decayed_at, last_reinforced_at,
      emotion_charge, uncertainty_score, last_emotion_inferred_at,
      created_at, updated_at
    FROM memories
    WHERE memory_id = :mid
""").bindparams(bindparam("mid", type_=PortableUUID(as_uuid=True)))

_LIST_COUNT = text("""
    SELECT count(*) FROM memories
    WHERE (:pid IS NULL OR project_id = :pid)
      AND (:store_id IS NULL OR store_id = :store_id)
      AND (:status IS NULL OR status = :status)
      AND (:slevel IS NULL OR sensitivity_level = :slevel)
      AND (:node_type IS NULL OR node_type = :node_type)
      AND (:search IS NULL OR (
            title LIKE '%' || :search || '%'
         OR memory_text LIKE '%' || :search || '%'
         OR canonical_key LIKE '%' || :search || '%'
      ))
""").bindparams(
    bindparam("pid", type_=PortableUUID(as_uuid=True)),
    bindparam("store_id", type_=PortableUUID(as_uuid=True)),
)

_LIST_QUERY = text("""
    SELECT
      memory_id, project_id, store_id, canonical_key,
      title, memory_text, current_version,
      sensitivity_level, status,
      node_type,
      activated_from_candidate_id, activated_by_review_item_id, activated_at,
      expired_at,
      quality_score, search_weight, last_refined_at,
      decay_score, decay_state, last_decayed_at, last_reinforced_at,
      emotion_charge, uncertainty_score, last_emotion_inferred_at,
      created_at, updated_at
    FROM memories
    WHERE (:pid IS NULL OR project_id = :pid)
      AND (:store_id IS NULL OR store_id = :store_id)
      AND (:status IS NULL OR status = :status)
      AND (:slevel IS NULL OR sensitivity_level = :slevel)
      AND (:node_type IS NULL OR node_type = :node_type)
      AND (:search IS NULL OR (
            title LIKE '%' || :search || '%'
         OR memory_text LIKE '%' || :search || '%'
         OR canonical_key LIKE '%' || :search || '%'
      ))
    ORDER BY updated_at DESC
    LIMIT :page_size OFFSET :offset
""").bindparams(
    bindparam("pid", type_=PortableUUID(as_uuid=True)),
    bindparam("store_id", type_=PortableUUID(as_uuid=True)),
)

# Fallback queries for SQLite where store_id column may not exist.
# PG_UUID bindparams ensure UUID objects are converted to strings for SQLite.
_LIST_COUNT_NO_STORE_ID = text("""
    SELECT count(*) FROM memories
    WHERE (:pid IS NULL OR project_id = :pid)
      AND (:status IS NULL OR status = :status)
      AND (:slevel IS NULL OR sensitivity_level = :slevel)
      AND (:node_type IS NULL OR node_type = :node_type)
      AND (:search IS NULL OR (
            title LIKE '%' || :search || '%'
         OR memory_text LIKE '%' || :search || '%'
         OR canonical_key LIKE '%' || :search || '%'
      ))
""").bindparams(
    bindparam("pid", type_=PortableUUID(as_uuid=True)),
)

_LIST_QUERY_NO_STORE_ID = text("""
    SELECT
      memory_id, project_id, canonical_key,
      title, memory_text, current_version,
      sensitivity_level, status,
      node_type,
      activated_from_candidate_id, activated_by_review_item_id, activated_at,
      expired_at,
      quality_score, search_weight, last_refined_at,
      decay_score, decay_state, last_decayed_at, last_reinforced_at,
      emotion_charge, uncertainty_score, last_emotion_inferred_at,
      created_at, updated_at
    FROM memories
    WHERE (:pid IS NULL OR project_id = :pid)
      AND (:status IS NULL OR status = :status)
      AND (:slevel IS NULL OR sensitivity_level = :slevel)
      AND (:node_type IS NULL OR node_type = :node_type)
      AND (:search IS NULL OR (
            title LIKE '%' || :search || '%'
         OR memory_text LIKE '%' || :search || '%'
         OR canonical_key LIKE '%' || :search || '%'
      ))
    ORDER BY updated_at DESC
    LIMIT :page_size OFFSET :offset
""").bindparams(
    bindparam("pid", type_=PortableUUID(as_uuid=True)),
)


_UPDATE_MEMORY = text("""
    UPDATE memories
    SET title = COALESCE(:title, title),
        memory_text = COALESCE(:mtext, memory_text),
        sensitivity_level = COALESCE(:slevel, sensitivity_level),
        store_id = COALESCE(:store_id, store_id),
        node_type = COALESCE(:node_type, node_type),
        current_version = :ver,
        updated_at = now()
    WHERE memory_id = :mid
      AND current_version = :ver - 1
    RETURNING
      memory_id, project_id, store_id, canonical_key,
      title, memory_text, current_version,
      sensitivity_level, status,
      node_type,
      activated_from_candidate_id, activated_by_review_item_id, activated_at,
      expired_at,
      quality_score, search_weight, last_refined_at,
      decay_score, decay_state, last_decayed_at, last_reinforced_at,
      emotion_charge, uncertainty_score, last_emotion_inferred_at,
      created_at, updated_at
""").bindparams(
    bindparam("mid", type_=PortableUUID(as_uuid=True)),
    bindparam("store_id", type_=PortableUUID(as_uuid=True)),
)

_UPDATE_STATUS = text("""
    UPDATE memories
    SET status = :status,
        expired_at = :expired_at,
        current_version = :ver,
        updated_at = now()
    WHERE memory_id = :mid
      AND status = :from_status
      AND current_version = :ver - 1
    RETURNING
      memory_id, project_id, store_id, canonical_key,
      title, memory_text, current_version,
      sensitivity_level, status,
      node_type,
      activated_from_candidate_id, activated_by_review_item_id, activated_at,
      expired_at,
      quality_score, search_weight, last_refined_at,
      decay_score, decay_state, last_decayed_at, last_reinforced_at,
      emotion_charge, uncertainty_score, last_emotion_inferred_at,
      created_at, updated_at
""").bindparams(
    bindparam("mid", type_=PortableUUID(as_uuid=True)),
)

_MERGE_UPDATE_SURVIVOR = text("""
    UPDATE memories
    SET memory_text = COALESCE(:mtext, memory_text),
        current_version = :ver,
        updated_at = now()
    WHERE memory_id = :mid
      AND current_version = :ver - 1
    RETURNING
      memory_id, project_id, store_id, canonical_key,
      title, memory_text, current_version,
      sensitivity_level, status,
      node_type,
      activated_from_candidate_id, activated_by_review_item_id, activated_at,
      expired_at,
      quality_score, search_weight, last_refined_at,
      decay_score, decay_state, last_decayed_at, last_reinforced_at,
      emotion_charge, uncertainty_score, last_emotion_inferred_at,
      created_at, updated_at
""").bindparams(
    bindparam("mid", type_=PortableUUID(as_uuid=True)),
)

_MERGE_UPDATE_CONSUMED = text("""
    UPDATE memories
    SET status = 'merged',
        current_version = :ver,
        expired_at = now(),
        updated_at = now()
    WHERE memory_id = :mid
      AND status IN ('draft', 'active')
      AND current_version = :ver - 1
    RETURNING
      memory_id, project_id, store_id, canonical_key,
      title, memory_text, current_version,
      sensitivity_level, status,
      node_type,
      activated_from_candidate_id, activated_by_review_item_id, activated_at,
      expired_at,
      quality_score, search_weight, last_refined_at,
      decay_score, decay_state, last_decayed_at, last_reinforced_at,
      emotion_charge, uncertainty_score, last_emotion_inferred_at,
      created_at, updated_at
""").bindparams(
    bindparam("mid", type_=PortableUUID(as_uuid=True)),
)


# ═══════════════════════════════════════════════════════════════════════
# Row mapping
# ═══════════════════════════════════════════════════════════════════════

def _memory_from_row(row: Any) -> MemoryRead:
    """Map a SQLAlchemy row to MemoryRead, normalizing JSONB fields."""
    data = dict(row._mapping)
    return MemoryRead.model_validate(data)


def _memory_data(m: MemoryRead) -> dict[str, Any]:
    """Extract key content fields as a plain dict for version snapshots."""
    return {
        "title": m.title,
        "memory_text": m.memory_text,
        "sensitivity_level": m.sensitivity_level,
        "status": m.status,
        "node_type": m.node_type,
    }


# ═══════════════════════════════════════════════════════════════════════
# P6 helpers — active memories with embeddings / quality score updates
# ═══════════════════════════════════════════════════════════════════════

def list_active_memories_with_embeddings(
    db: Session,
    *,
    project_id: UUID,
) -> list[dict[str, Any]]:
    """Return active memories with their embedding vectors for dedup scanning.

    Joins memories with memory_index_entries to get embedding data.
    Only returns memories with ``status='active'`` and
    ``vector_state='ready'``.
    """
    from mneme.memory.search import _parse_stored_embedding

    rows = db.execute(
        text("""
            SELECT m.memory_id, m.canonical_key, m.title, m.memory_text,
                   m.quality_score, m.search_weight,
                   mie.embedding, mie.memory_index_entry_id
            FROM memories m
            JOIN memory_index_entries mie ON mie.memory_id = m.memory_id
            WHERE m.project_id = :pid
              AND m.status = 'active'
              AND mie.vector_state = 'ready'
              AND mie.embedding IS NOT NULL
            ORDER BY m.created_at
        """),
        {"pid": project_id},
    ).all()

    results: list[dict[str, Any]] = []
    for row in rows:
        d: dict[str, Any] = dict(row._mapping)
        parsed = _parse_stored_embedding(d.get("embedding"))
        if parsed is not None:
            d["embedding_vector"] = parsed
            del d["embedding"]
            results.append(d)
    return results


def update_quality_score(
    db: Session,
    context: RequestContext,
    *,
    memory_id: UUID,
    quality_score: float,
    search_weight: float,
) -> MemoryRead:
    """Update *quality_score*, *search_weight*, and *last_refined_at*.

    Uses optimistic locking (``current_version``) + audit/outbox.
    """
    existing = get_memory(db, memory_id)
    if existing is None:
        raise ValueError(f"memory {memory_id} not found")

    new_ver = existing.current_version + 1

    outbox_event = OutboxEvent(
        event_type="memory.quality_updated",
        aggregate_type="memory",
        aggregate_id=memory_id,
        aggregate_version=new_ver,
        idempotency_key=f"{context.idempotency_key or ''}:quality:{memory_id}",
        producer="mneme-api",
        payload_json={
            "memory_id": str(memory_id),
            "quality_score": quality_score,
            "search_weight": search_weight,
        },
    )
    audit_event = AuditEvent(
        action="memory.quality",
        result="success",
        object_type="memory",
        object_id=memory_id,
        project_id=existing.project_id,
        sensitivity_level=existing.sensitivity_level,
        diff_summary={
            "quality_score": quality_score,
            "search_weight": search_weight,
        },
    )

    def _do_update(db: Session) -> MemoryRead:
        row = db.execute(
            text("""
                UPDATE memories
                SET quality_score = :qs,
                    search_weight = :sw,
                    last_refined_at = CURRENT_TIMESTAMP,
                    current_version = :new_ver,
                    updated_at = CURRENT_TIMESTAMP
                WHERE memory_id = :mid AND current_version = :cur_ver
                RETURNING *
            """),
            {
                "qs": quality_score,
                "sw": search_weight,
                "new_ver": new_ver,
                "mid": memory_id,
                "cur_ver": existing.current_version,
            },
        ).first()
        if row is None:
            raise ValueError(f"concurrent update on memory {memory_id}")
        return _memory_from_row(row)

    return write_with_audit_outbox_idempotency(
        db,
        context=context,
        work=_do_update,
        audit_event=audit_event,
        outbox_event=outbox_event,
    )


# ═══════════════════════════════════════════════════════════════════════
# Internal — auto-create graph relations (best-effort, non-fatal)
# ═══════════════════════════════════════════════════════════════════════


def _try_auto_create_relations(
    db: Session,
    context: RequestContext,
    memory_id: UUID,
) -> None:
    """Best-effort auto-creation of graph edges for a memory.

    Called after ``activate_memory`` and ``approve_memory`` to link the
    newly active memory to existing active memories via similarity, temporal,
    causal, and heuristic edges.  Failures are logged but never propagated —
    the memory activation/approval is considered successful regardless.
    """
    try:
        from mneme.memory.graph_relations import auto_create_relations_smart
        result = auto_create_relations_smart(db, context, memory_id=memory_id)
        logger.info(
            "graph_relations: auto-created %d edges for memory %s (%s)",
            result.total_edges, memory_id, result.edges_summary,
        )
    except Exception:
        logger.debug(
            "graph_relations: auto-create failed for memory %s",
            memory_id, exc_info=True,
        )


# ═══════════════════════════════════════════════════════════════════════
# Public API — activate (from candidate)
# ═══════════════════════════════════════════════════════════════════════

def activate_memory(
    db: Session,
    context: RequestContext,
    *,
    candidate_id: UUID,
    project_id: UUID,
    title: str | None,
    memory_text: str,
    sensitivity_level: str,
    review_item_id: UUID,
    node_type: str | None = None,
) -> MemoryRead:
    """Create an active memory from an approved candidate.

    Writes ``memories`` + ``memory_versions`` (v1, create) +
    ``memory_sources`` (origin) in one transaction.
    """
    memory_id = uuid4()
    canonical_key = _generate_canonical_key(db, project_id)
    now = datetime.now(timezone.utc)

    outbox_event = OutboxEvent(
        event_type="memory.activated",
        aggregate_type="memory",
        aggregate_id=memory_id,
        aggregate_version=1,
        idempotency_key=context.idempotency_key or str(uuid4()),
        producer="mneme-api",
        payload_json={
            "memory_id": str(memory_id),
            "candidate_id": str(candidate_id),
            "canonical_key": canonical_key,
            "review_item_id": str(review_item_id),
            "node_type": node_type,
        },
    )

    audit_event = AuditEvent(
        action="memory.activate",
        result="success",
        object_type="memory",
        object_id=memory_id,
        project_id=project_id,
        sensitivity_level=sensitivity_level,
        review_item_id=review_item_id,
        diff_summary={
            "candidate_id": str(candidate_id),
            "canonical_key": canonical_key,
            "node_type": node_type,
        },
    )

    def _do_insert(db: Session) -> MemoryRead:
        row = db.execute(
            _INSERT_MEMORY,
            {
                "mid": memory_id,
                "pid": project_id,
                "ckey": canonical_key,
                "title": title,
                "mtext": memory_text,
                "store_id": None,
                "ver": 1,
                "slevel": sensitivity_level,
                "status": "active",
                "node_type": node_type,
                "afcid": candidate_id,
                "arid": review_item_id,
                "aat": now,
            },
        ).one()
        mem = _memory_from_row(row)

        # Record version v1
        _record_memory_version(
            db,
            memory_id=memory_id,
            version=1,
            action="create",
            before_json={},
            after_json=_memory_data(mem),
            actor_type=context.actor.actor_type,
            actor_id=context.actor.actor_id,
            review_item_id=review_item_id,
        )

        # Record source (origin candidate)
        _record_memory_source(
            db,
            memory_id=memory_id,
            version=1,
            candidate_id=candidate_id,
        )

        # Create FTS index entry (P4-07)
        try:
            from mneme.memory.index_manager import on_memory_activated
            on_memory_activated(
                db,
                memory_id=memory_id,
                version=1,
                project_id=project_id,
                title=title,
                memory_text=memory_text,
            )
        except Exception:
            logger.debug("Failed to create memory index entry", exc_info=True)

        return mem

    def _resolve_existing(_db: Session, _aggregate_id: UUID) -> MemoryRead:
        m = get_memory(_db, _aggregate_id)
        if m is None:
            raise LookupError(f"memory {_aggregate_id} not found during idempotent replay")
        return m

    result = write_with_audit_outbox_idempotency(
        db,
        context,
        work=_do_insert,
        audit_event=audit_event,
        outbox_event=outbox_event,
        resolve_existing=_resolve_existing,
    )

    # Auto-create graph relations for the newly activated memory
    _try_auto_create_relations(db, context, result.memory_id)

    return result


# ═══════════════════════════════════════════════════════════════════════
# Public API — create (manual)
# ═══════════════════════════════════════════════════════════════════════

def create_memory(
    db: Session,
    context: RequestContext,
    *,
    payload: MemoryCreate,
) -> MemoryRead:
    """Create a memory manually (status='draft', not yet active)."""
    memory_id = uuid4()
    canonical_key = (
        payload.canonical_key
        if payload.canonical_key
        else _generate_canonical_key(db, payload.project_id)
    )

    outbox_event = OutboxEvent(
        event_type="memory.created",
        aggregate_type="memory",
        aggregate_id=memory_id,
        aggregate_version=1,
        idempotency_key=context.idempotency_key or str(uuid4()),
        producer="mneme-api",
        payload_json={
            "memory_id": str(memory_id),
            "project_id": str(payload.project_id),
            "canonical_key": canonical_key,
        },
    )

    audit_event = AuditEvent(
        action="memory.create",
        result="success",
        object_type="memory",
        object_id=memory_id,
        project_id=payload.project_id,
        sensitivity_level=payload.sensitivity_level,
        diff_summary={"status": "draft", "canonical_key": canonical_key},
    )

    def _do_insert(db: Session) -> MemoryRead:
        row = db.execute(
            _INSERT_MEMORY,
            {
                "mid": memory_id,
                "pid": payload.project_id,
                "ckey": canonical_key,
                "title": payload.title,
                "mtext": payload.memory_text,
                "store_id": payload.store_id,
                "ver": 1,
                "slevel": payload.sensitivity_level,
                "status": "draft",
                "node_type": payload.node_type.value if payload.node_type else None,
                "afcid": None,
                "arid": None,
                "aat": None,
            },
        ).one()
        mem = _memory_from_row(row)

        _record_memory_version(
            db,
            memory_id=memory_id,
            version=1,
            action="create",
            before_json={},
            after_json=_memory_data(mem),
            actor_type=context.actor.actor_type,
            actor_id=context.actor.actor_id,
        )

        # Create FTS index entry (P4-07)
        try:
            from mneme.memory.index_manager import on_memory_activated
            on_memory_activated(
                db,
                memory_id=memory_id,
                version=1,
                project_id=payload.project_id,
                title=payload.title,
                memory_text=payload.memory_text,
            )
        except Exception:
            logger.debug("Failed to create memory index entry", exc_info=True)

        return mem

    def _resolve_existing(_db: Session, _aggregate_id: UUID) -> MemoryRead:
        m = get_memory(_db, _aggregate_id)
        if m is None:
            raise LookupError(f"memory {_aggregate_id} not found during idempotent replay")
        return m

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

def get_memory(db: Session, memory_id: UUID) -> MemoryRead | None:
    """Look up a memory by primary key."""
    row = db.execute(_SELECT_MEMORY, {"mid": memory_id}).first()
    if row is None:
        return None
    return _memory_from_row(row)


def list_memories(
    db: Session,
    *,
    project_id: UUID | None = None,
    store_id: UUID | None = None,
    status: str | None = None,
    sensitivity_level: str | None = None,
    search: str | None = None,
    node_type: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[MemoryRead], int]:
    """List memories with filters and pagination.

    The ``store_id`` filter is only applied when the column exists in the
    database (always in PostgreSQL; conditionally in SQLite).
    """
    has_store = _has_store_id_column(db)
    count_query = _LIST_COUNT if has_store else _LIST_COUNT_NO_STORE_ID
    list_query = _LIST_QUERY if has_store else _LIST_QUERY_NO_STORE_ID

    params: dict[str, Any] = {
        "pid": project_id,
        "status": status,
        "slevel": sensitivity_level,
        "search": search,
        "node_type": node_type,
    }
    if has_store:
        params["store_id"] = store_id

    total = db.execute(count_query, params).scalar_one()
    offset = (page - 1) * page_size
    rows = db.execute(
        list_query,
        {**params, "page_size": page_size, "offset": offset},
    ).all()
    items = [_memory_from_row(row) for row in rows]
    return items, total


# ═══════════════════════════════════════════════════════════════════════
# Public API — update
# ═══════════════════════════════════════════════════════════════════════

def update_memory(
    db: Session,
    context: RequestContext,
    *,
    memory_id: UUID,
    payload: MemoryUpdate,
) -> MemoryRead:
    """Update mutable fields of a memory. Increments version, records version row."""
    existing = get_memory(db, memory_id)
    if existing is None:
        raise ValueError(f"memory {memory_id} not found")

    new_version = existing.current_version + 1
    before = _memory_data(existing)

    outbox_event = OutboxEvent(
        event_type="memory.updated",
        aggregate_type="memory",
        aggregate_id=memory_id,
        aggregate_version=new_version,
        idempotency_key=f"{context.idempotency_key or ''}:update:{memory_id}",
        producer="mneme-api",
        payload_json={
            "memory_id": str(memory_id),
            "version": new_version,
            "fields": payload.model_dump(exclude_none=True),
        },
    )

    audit_event = AuditEvent(
        action="memory.update",
        result="success",
        object_type="memory",
        object_id=memory_id,
        project_id=existing.project_id,
        sensitivity_level=existing.sensitivity_level,
        diff_summary=payload.model_dump(exclude_none=True),
    )

    def _do_update(db: Session) -> MemoryRead:
        row = db.execute(
            _UPDATE_MEMORY,
            {
                "mid": memory_id,
                "title": payload.title,
                "mtext": payload.memory_text,
                "slevel": payload.sensitivity_level,
                "store_id": payload.store_id,
                "node_type": payload.node_type.value if payload.node_type else None,
                "ver": new_version,
            },
        ).first()
        if row is None:
            raise ValueError(
                f"memory {memory_id} concurrent modification detected"
            )
        mem = _memory_from_row(row)

        # Record version
        _record_memory_version(
            db,
            memory_id=memory_id,
            version=new_version,
            action="update",
            before_json=before,
            after_json=_memory_data(mem),
            actor_type=context.actor.actor_type,
            actor_id=context.actor.actor_id,
        )

        # Update FTS index: mark old stale, create new entry (P4-07)
        try:
            from mneme.memory.index_manager import on_memory_updated
            on_memory_updated(
                db,
                memory_id=memory_id,
                old_version=existing.current_version,
                new_version=new_version,
                project_id=mem.project_id,
                title=mem.title,
                memory_text=mem.memory_text,
            )
        except Exception:
            logger.debug("Failed to update memory index entry", exc_info=True)

        return mem

    def _resolve_existing(_db: Session, _aggregate_id: UUID) -> MemoryRead:
        m = get_memory(_db, _aggregate_id)
        if m is None:
            raise LookupError(f"memory {_aggregate_id} not found")
        return m

    return write_with_audit_outbox_idempotency(
        db,
        context,
        work=_do_update,
        audit_event=audit_event,
        outbox_event=outbox_event,
        resolve_existing=_resolve_existing,
    )


# ═══════════════════════════════════════════════════════════════════════
# Public API — merge
# ═══════════════════════════════════════════════════════════════════════

def merge_memory(
    db: Session,
    context: RequestContext,
    *,
    memory_id: UUID,
    payload: MemoryMerge,
) -> MemoryRead:
    """Merge ``target_memory_id`` into ``memory_id``.

    ``memory_id`` (survivor) absorbs ``target_memory_id`` (consumed).
    The consumed memory's text is appended to the survivor.
    Consumed gets ``status='merged'``.
    Creates ``merged_into`` relation: consumed → survivor.
    """
    survivor = get_memory(db, memory_id)
    if survivor is None:
        raise ValueError(f"survivor memory {memory_id} not found")
    if survivor.status not in ("draft", "active"):
        raise ValueError(
            f"memory {memory_id} is '{survivor.status}', "
            f"only draft/active can receive merge"
        )

    consumed = get_memory(db, payload.target_memory_id)
    if consumed is None:
        raise ValueError(f"target memory {payload.target_memory_id} not found")
    if consumed.status not in ("draft", "active"):
        raise ValueError(
            f"target memory {payload.target_memory_id} is '{consumed.status}', "
            f"only draft/active can be merged"
        )
    if memory_id == payload.target_memory_id:
        raise ValueError("cannot merge a memory into itself")

    survivor_new_ver = survivor.current_version + 1
    consumed_new_ver = consumed.current_version + 1
    before_survivor = _memory_data(survivor)
    before_consumed = _memory_data(consumed)

    # Survivor absorbs consumed text
    merged_text = (
        f"{survivor.memory_text}\n\n--- Merged from {consumed.canonical_key} ---\n"
        f"{consumed.memory_text}"
    )

    outbox_event = OutboxEvent(
        event_type="memory.merged",
        aggregate_type="memory",
        aggregate_id=memory_id,
        aggregate_version=survivor_new_ver,
        idempotency_key=f"{context.idempotency_key or ''}:merge:{memory_id}",
        producer="mneme-api",
        payload_json={
            "survivor_memory_id": str(memory_id),
            "consumed_memory_id": str(payload.target_memory_id),
        },
    )

    audit_event = AuditEvent(
        action="memory.merge",
        result="success",
        object_type="memory",
        object_id=memory_id,
        project_id=survivor.project_id,
        sensitivity_level=survivor.sensitivity_level,
        diff_summary={
            "consumed_memory_id": str(payload.target_memory_id),
            "reason": payload.reason,
        },
    )

    def _do_merge(db: Session) -> MemoryRead:
        # 1. Update survivor (absorb consumed text)
        srow = db.execute(
            _MERGE_UPDATE_SURVIVOR,
            {
                "mid": memory_id,
                "mtext": merged_text,
                "ver": survivor_new_ver,
            },
        ).first()
        if srow is None:
            raise ValueError(
                f"survivor memory {memory_id} concurrent modification detected"
            )
        survivor_after = _memory_from_row(srow)

        # 2. Mark consumed as merged
        trow = db.execute(
            _MERGE_UPDATE_CONSUMED,
            {
                "mid": payload.target_memory_id,
                "ver": consumed_new_ver,
            },
        ).first()
        if trow is None:
            raise ValueError(
                f"consumed memory {payload.target_memory_id} not in draft/active "
                f"or concurrent modification detected"
            )
        consumed_after = _memory_from_row(trow)

        # 3. Record version for survivor
        _record_memory_version(
            db,
            memory_id=memory_id,
            version=survivor_new_ver,
            action="merge",
            before_json=before_survivor,
            after_json=_memory_data(survivor_after),
            actor_type=context.actor.actor_type,
            actor_id=context.actor.actor_id,
            reason=payload.reason,
        )

        # Update FTS index for survivor (content changed) — P4-07
        try:
            from mneme.memory.index_manager import on_memory_updated
            on_memory_updated(
                db,
                memory_id=memory_id,
                old_version=survivor.current_version,
                new_version=survivor_new_ver,
                project_id=survivor_after.project_id,
                title=survivor_after.title,
                memory_text=survivor_after.memory_text,
            )
        except Exception:
            logger.debug("Failed to update memory index on merge", exc_info=True)

        # 4. Record version for consumed
        _record_memory_version(
            db,
            memory_id=payload.target_memory_id,
            version=consumed_new_ver,
            action="merge",
            before_json=before_consumed,
            after_json=_memory_data(consumed_after),
            actor_type=context.actor.actor_type,
            actor_id=context.actor.actor_id,
            reason=payload.reason,
        )

        # 5. Create merged_into relation: consumed → survivor
        db.execute(
            _INSERT_RELATION,
            {
                "rid": uuid4(),
                "pid": survivor.project_id,
                "from_mid": payload.target_memory_id,
                "from_ver": consumed_new_ver,
                "to_mid": memory_id,
                "to_ver": survivor_new_ver,
                "reason": payload.reason,
            },
        )

        return survivor_after

    def _resolve_existing(_db: Session, _aggregate_id: UUID) -> MemoryRead:
        m = get_memory(_db, _aggregate_id)
        if m is None:
            raise LookupError(f"memory {_aggregate_id} not found")
        return m

    return write_with_audit_outbox_idempotency(
        db,
        context,
        work=_do_merge,
        audit_event=audit_event,
        outbox_event=outbox_event,
        resolve_existing=_resolve_existing,
    )


# ═══════════════════════════════════════════════════════════════════════
# Public API — expire / restore / delete
# ═══════════════════════════════════════════════════════════════════════

def _transition_status(
    db: Session,
    context: RequestContext,
    *,
    memory_id: UUID,
    from_status: str,
    to_status: str,
    action: str,
    tag: str,
) -> MemoryRead:
    """Generic status transition with version recording."""
    existing = get_memory(db, memory_id)
    if existing is None:
        raise ValueError(f"memory {memory_id} not found")
    if existing.status != from_status:
        raise ValueError(
            f"memory {memory_id} is '{existing.status}', expected '{from_status}'"
        )

    new_version = existing.current_version + 1
    before = _memory_data(existing)

    # Compute expired_at for the transition
    if to_status == "expired":
        actual_expire = datetime.now(timezone.utc)
    elif to_status == "active":
        actual_expire = None  # clear on restore
    else:
        actual_expire = existing.expired_at  # preserve for delete etc.

    outbox_event = OutboxEvent(
        event_type=f"memory.{tag}",
        aggregate_type="memory",
        aggregate_id=memory_id,
        aggregate_version=new_version,
        idempotency_key=f"{context.idempotency_key or ''}:{tag}:{memory_id}",
        producer="mneme-api",
        payload_json={
            "memory_id": str(memory_id),
            "from_status": from_status,
            "to_status": to_status,
        },
    )

    audit_event = AuditEvent(
        action=f"memory.{tag}",
        result="success",
        object_type="memory",
        object_id=memory_id,
        project_id=existing.project_id,
        sensitivity_level=existing.sensitivity_level,
        diff_summary={"status": f"{from_status}→{to_status}"},
    )

    def _do_transition(db: Session) -> MemoryRead:
        row = db.execute(
            _UPDATE_STATUS,
            {
                "mid": memory_id,
                "status": to_status,
                "expired_at": actual_expire,
                "ver": new_version,
                "from_status": from_status,
            },
        ).first()
        if row is None:
            raise ValueError(
                f"memory {memory_id} state transition {from_status}→{to_status} failed "
                f"(concurrent modification)"
            )
        mem = _memory_from_row(row)

        _record_memory_version(
            db,
            memory_id=memory_id,
            version=new_version,
            action=action,
            before_json=before,
            after_json=_memory_data(mem),
            actor_type=context.actor.actor_type,
            actor_id=context.actor.actor_id,
        )

        # P5-01: index_manager hooks for expire/restore/delete
        from mneme.memory.index_manager import (
            on_memory_deleted,
            on_memory_expired,
            on_memory_restored,
        )

        try:
            if to_status == "expired":
                on_memory_expired(db, memory_id=memory_id)
            elif to_status == "deleted":
                on_memory_deleted(db, memory_id=memory_id)
            elif to_status == "active" and from_status in ("expired", "deleted"):
                on_memory_restored(
                    db,
                    memory_id=memory_id,
                    version=new_version,
                    project_id=mem.project_id,
                    title=mem.title,
                    memory_text=mem.memory_text,
                )
        except Exception:
            logger.debug(
                "Failed to update index on %s→%s for memory %s",
                from_status, to_status, memory_id,
                exc_info=True,
            )

        return mem

    def _resolve_existing(_db: Session, _aggregate_id: UUID) -> MemoryRead:
        m = get_memory(_db, _aggregate_id)
        if m is None:
            raise LookupError(f"memory {_aggregate_id} not found")
        return m

    return write_with_audit_outbox_idempotency(
        db,
        context,
        work=_do_transition,
        audit_event=audit_event,
        outbox_event=outbox_event,
        resolve_existing=_resolve_existing,
    )


def expire_memory(
    db: Session,
    context: RequestContext,
    *,
    memory_id: UUID,
) -> MemoryRead:
    """Expire a memory: active → expired."""
    return _transition_status(
        db,
        context,
        memory_id=memory_id,
        from_status="active",
        to_status="expired",
        action="expire",
        tag="expired",
    )


def restore_memory(
    db: Session,
    context: RequestContext,
    *,
    memory_id: UUID,
) -> MemoryRead:
    """Restore a memory: expired|deleted → active."""
    existing = get_memory(db, memory_id)
    if existing is None:
        raise ValueError(f"memory {memory_id} not found")
    if existing.status not in ("expired", "deleted"):
        raise ValueError(
            f"memory {memory_id} is '{existing.status}', expected 'expired' or 'deleted'"
        )
    return _transition_status(
        db,
        context,
        memory_id=memory_id,
        from_status=existing.status,
        to_status="active",
        action="restore",
        tag="restored",
    )


def delete_memory(
    db: Session,
    context: RequestContext,
    *,
    memory_id: UUID,
) -> MemoryRead:
    """Soft-delete a memory: any status → deleted."""
    existing = get_memory(db, memory_id)
    if existing is None:
        raise ValueError(f"memory {memory_id} not found")
    if existing.status == "deleted":
        raise ValueError(f"memory {memory_id} is already deleted")
    return _transition_status(
        db,
        context,
        memory_id=memory_id,
        from_status=existing.status,
        to_status="deleted",
        action="delete",
        tag="deleted",
    )


# ═══════════════════════════════════════════════════════════════════════
# Public API — batch approve / reject
# ═══════════════════════════════════════════════════════════════════════

def approve_memory(
    db: Session,
    context: RequestContext,
    *,
    memory_id: UUID,
    review_item_id: UUID | None = None,
    reason: str | None = None,
) -> MemoryRead:
    """Approve a draft memory: draft → active.

    Sets ``activated_by_review_item_id`` if provided, so the CHECK constraint
    is satisfied.  Only ``draft`` memories can be approved.
    """
    existing = get_memory(db, memory_id)
    if existing is None:
        raise ValueError(f"memory {memory_id} not found")
    if existing.status != "draft":
        raise ValueError(
            f"memory {memory_id} is '{existing.status}', expected 'draft'"
        )

    new_version = existing.current_version + 1
    before = _memory_data(existing)
    now = datetime.now(timezone.utc)

    outbox_event = OutboxEvent(
        event_type="memory.approved",
        aggregate_type="memory",
        aggregate_id=memory_id,
        aggregate_version=new_version,
        idempotency_key=f"{context.idempotency_key or ''}:approve:{memory_id}",
        producer="mneme-api",
        payload_json={
            "memory_id": str(memory_id),
            "from_status": "draft",
            "to_status": "active",
            "reason": reason,
        },
    )

    audit_event = AuditEvent(
        action="memory.approve",
        result="success",
        object_type="memory",
        object_id=memory_id,
        project_id=existing.project_id,
        sensitivity_level=existing.sensitivity_level,
        review_item_id=review_item_id,
        diff_summary={
            "status": "draft→active",
            "reason": reason,
            "review_item_id": str(review_item_id) if review_item_id else None,
        },
    )

    def _do_approve(db: Session) -> MemoryRead:
        row = db.execute(
            text("""
                UPDATE memories
                SET status = 'active',
                    activated_at = COALESCE(activated_at, :aat),
                    activated_by_review_item_id = COALESCE(activated_by_review_item_id, :arid),
                    current_version = :ver,
                    updated_at = now()
                WHERE memory_id = :mid
                  AND status = 'draft'
                  AND current_version = :ver - 1
                RETURNING *
            """),
            {
                "mid": memory_id,
                "ver": new_version,
                "aat": existing.activated_at or now,
                "arid": review_item_id or existing.activated_by_review_item_id,
            },
        ).first()
        if row is None:
            raise ValueError(
                f"memory {memory_id} approve failed (concurrent modification)"
            )
        mem = _memory_from_row(row)

        _record_memory_version(
            db,
            memory_id=memory_id,
            version=new_version,
            action="approve",
            before_json=before,
            after_json=_memory_data(mem),
            actor_type=context.actor.actor_type,
            actor_id=context.actor.actor_id,
            review_item_id=review_item_id,
            reason=reason,
        )

        # Update FTS index (P4-07)
        try:
            from mneme.memory.index_manager import on_memory_restored
            on_memory_restored(
                db,
                memory_id=memory_id,
                version=new_version,
                project_id=mem.project_id,
                title=mem.title,
                memory_text=mem.memory_text,
            )
        except Exception:
            logger.debug("Failed to update memory index on approve", exc_info=True)

        return mem

    def _resolve_existing(_db: Session, _aggregate_id: UUID) -> MemoryRead:
        m = get_memory(_db, _aggregate_id)
        if m is None:
            raise LookupError(f"memory {_aggregate_id} not found")
        return m

    result = write_with_audit_outbox_idempotency(
        db,
        context,
        work=_do_approve,
        audit_event=audit_event,
        outbox_event=outbox_event,
        resolve_existing=_resolve_existing,
    )

    # Auto-create graph relations for the newly approved memory
    _try_auto_create_relations(db, context, result.memory_id)

    return result


def reject_memory(
    db: Session,
    context: RequestContext,
    *,
    memory_id: UUID,
    reason: str | None = None,
) -> MemoryRead:
    """Reject a draft memory: draft → deleted.

    Only ``draft`` memories can be rejected.
    """
    existing = get_memory(db, memory_id)
    if existing is None:
        raise ValueError(f"memory {memory_id} not found")
    if existing.status != "draft":
        raise ValueError(
            f"memory {memory_id} is '{existing.status}', expected 'draft'"
        )
    return _transition_status(
        db,
        context,
        memory_id=memory_id,
        from_status="draft",
        to_status="deleted",
        action="reject",
        tag="rejected",
    )


@dataclass
class BatchMemoryResult:
    """Aggregated result from a batch memory operation."""
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    results: list[dict[str, Any]] = field(default_factory=list)


def batch_approve_memories(
    db: Session,
    context: RequestContext,
    *,
    memory_ids: list[UUID],
    review_item_id: UUID | None = None,
    reason: str | None = None,
) -> BatchMemoryResult:
    """Batch approve multiple draft memories→active."""
    result = BatchMemoryResult(total=len(memory_ids))
    for mid in memory_ids:
        try:
            mem = approve_memory(
                db, context,
                memory_id=mid,
                review_item_id=review_item_id,
                reason=reason,
            )
            result.succeeded += 1
            result.results.append({
                "memory_id": str(mid),
                "status": "succeeded",
                "new_status": mem.status,
                "canonical_key": mem.canonical_key,
            })
        except ValueError as e:
            result.failed += 1
            result.results.append({
                "memory_id": str(mid),
                "status": "failed",
                "error": str(e),
            })
        except Exception as e:
            result.failed += 1
            result.results.append({
                "memory_id": str(mid),
                "status": "failed",
                "error": f"unexpected error: {e}",
            })
    return result


def batch_reject_memories(
    db: Session,
    context: RequestContext,
    *,
    memory_ids: list[UUID],
    reason: str | None = None,
) -> BatchMemoryResult:
    """Batch reject multiple draft memories→deleted."""
    result = BatchMemoryResult(total=len(memory_ids))
    for mid in memory_ids:
        try:
            mem = reject_memory(
                db, context,
                memory_id=mid,
                reason=reason,
            )
            result.succeeded += 1
            result.results.append({
                "memory_id": str(mid),
                "status": "succeeded",
                "new_status": mem.status,
                "canonical_key": mem.canonical_key,
            })
        except ValueError as e:
            result.failed += 1
            result.results.append({
                "memory_id": str(mid),
                "status": "failed",
                "error": str(e),
            })
        except Exception as e:
            result.failed += 1
            result.results.append({
                "memory_id": str(mid),
                "status": "failed",
                "error": f"unexpected error: {e}",
            })
    return result
