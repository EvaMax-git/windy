"""P5-04 Context Packs data-access layer.

Raw-SQL CRUD against ``context_packs`` and ``context_pack_items``.

DDL alignment
-------------
Table: ``context_packs`` (baseline L947-967)
Table: ``context_pack_items`` (baseline L969-986)
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Session

from mneme.api.context import RequestContext
from mneme.db.audit import AuditEvent, OutboxEvent, add_audit_event, add_outbox_event
from mneme.db.transactions import transaction


# ═══════════════════════════════════════════════════════════════════════
# SQL — context_packs INSERT
# ═══════════════════════════════════════════════════════════════════════

_INSERT_PACK = text("""
    INSERT INTO context_packs (
      context_pack_id, request_id, correlation_id,
      agent_id, project_id, actor_type, actor_id,
      compile_mode, status,
      knowledge_version_set, memory_version_set,
      token_budget, exclusion_summary,
      api_call_log_id
    ) VALUES (
      :pack_id, :request_id, :correlation_id,
      :agent_id, :project_id, :actor_type, :actor_id,
      :compile_mode, :status,
      :knowledge_version_set, :memory_version_set,
      :token_budget, :exclusion_summary,
      :api_call_log_id
    )
    RETURNING
      context_pack_id, request_id, correlation_id,
      agent_id, project_id, actor_type, actor_id,
      compile_mode, status,
      knowledge_version_set, memory_version_set,
      token_budget, exclusion_summary,
      api_call_log_id, retention_until, created_at
""").bindparams(
    bindparam("pack_id", type_=PG_UUID(as_uuid=True)),
    bindparam("request_id", type_=PG_UUID(as_uuid=True)),
    bindparam("correlation_id", type_=PG_UUID(as_uuid=True)),
    bindparam("agent_id", type_=PG_UUID(as_uuid=True)),
    bindparam("project_id", type_=PG_UUID(as_uuid=True)),
    bindparam("actor_id", type_=PG_UUID(as_uuid=True)),
    bindparam("api_call_log_id", type_=PG_UUID(as_uuid=True)),
    bindparam("knowledge_version_set", type_=JSONB),
    bindparam("memory_version_set", type_=JSONB),
    bindparam("token_budget", type_=JSONB),
    bindparam("exclusion_summary", type_=JSONB),
)


# ═══════════════════════════════════════════════════════════════════════
# SQL — context_pack_items INSERT
# ═══════════════════════════════════════════════════════════════════════

_INSERT_ITEM = text("""
    INSERT INTO context_pack_items (
      context_pack_item_id, context_pack_id,
      item_order, item_type,
      object_id, object_version,
      source_ref, included, exclusion_reason,
      score, token_count, reason, content_digest
    ) VALUES (
      :item_id, :pack_id,
      :item_order, :item_type,
      :object_id, :object_version,
      :source_ref, :included, :exclusion_reason,
      :score, :token_count, :reason, :content_digest
    )
    RETURNING
      context_pack_item_id, context_pack_id,
      item_order, item_type,
      object_id, object_version,
      source_ref, included, exclusion_reason,
      score, token_count, reason, content_digest,
      created_at
""").bindparams(
    bindparam("item_id", type_=PG_UUID(as_uuid=True)),
    bindparam("pack_id", type_=PG_UUID(as_uuid=True)),
    bindparam("object_id", type_=PG_UUID(as_uuid=True)),
    bindparam("source_ref", type_=JSONB),
)


# ═══════════════════════════════════════════════════════════════════════
# SQL — SELECT (read paths)
# ═══════════════════════════════════════════════════════════════════════

_SELECT_PACK_BY_ID = text("""
    SELECT
      context_pack_id, request_id, correlation_id,
      agent_id, project_id, actor_type, actor_id,
      compile_mode, status,
      knowledge_version_set, memory_version_set,
      token_budget, exclusion_summary,
      api_call_log_id, retention_until, created_at
    FROM context_packs
    WHERE context_pack_id = :pack_id
""").bindparams(bindparam("pack_id", type_=PG_UUID(as_uuid=True)))

_SELECT_ITEMS_BY_PACK = text("""
    SELECT
      context_pack_item_id, context_pack_id,
      item_order, item_type,
      object_id, object_version,
      source_ref, included, exclusion_reason,
      score, token_count, reason, content_digest,
      created_at
    FROM context_pack_items
    WHERE context_pack_id = :pack_id
    ORDER BY item_order ASC
""").bindparams(bindparam("pack_id", type_=PG_UUID(as_uuid=True)))

_LIST_PACKS_COUNT = text("""
    SELECT count(*)
    FROM context_packs
    WHERE (:agent_id IS NULL OR agent_id = :agent_id)
      AND (:project_id IS NULL OR project_id = :project_id)
      AND (:status IS NULL OR status = :status)
""").bindparams(
    bindparam("agent_id", type_=PG_UUID(as_uuid=True)),
    bindparam("project_id", type_=PG_UUID(as_uuid=True)),
)

_LIST_PACKS = text("""
    SELECT
      context_pack_id, request_id, correlation_id,
      agent_id, project_id, actor_type, actor_id,
      compile_mode, status,
      knowledge_version_set, memory_version_set,
      token_budget, exclusion_summary,
      api_call_log_id, retention_until, created_at
    FROM context_packs
    WHERE (:agent_id IS NULL OR agent_id = :agent_id)
      AND (:project_id IS NULL OR project_id = :project_id)
      AND (:status IS NULL OR status = :status)
    ORDER BY created_at DESC
    LIMIT :page_size OFFSET :offset
""").bindparams(
    bindparam("agent_id", type_=PG_UUID(as_uuid=True)),
    bindparam("project_id", type_=PG_UUID(as_uuid=True)),
)


# ═══════════════════════════════════════════════════════════════════════
# Row mapping
# ═══════════════════════════════════════════════════════════════════════

def _as_uuid(value: Any) -> UUID | None:
    """Coerce a value to :class:`uuid.UUID`.

    PostgreSQL's psycopg2 driver returns Python ``UUID`` objects natively.
    SQLite returns hex strings (without dashes) for UUID columns.
    """
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


_UUID_FIELDS_PACK = (
    "context_pack_id", "request_id", "correlation_id",
    "agent_id", "project_id", "actor_id", "api_call_log_id",
)


_UUID_FIELDS_ITEM = ("context_pack_item_id", "context_pack_id", "object_id")


def _pack_from_row(row: Any) -> dict[str, Any]:
    """Map a context_packs row to a dict, coercing JSONB and UUID fields."""
    data = dict(row._mapping)
    # Coerce UUID fields (SQLite returns hex strings without dashes)
    for field in _UUID_FIELDS_PACK:
        if field in data:
            data[field] = _as_uuid(data[field])
    # Coerce JSONB fields (SQLite returns plain strings)
    for field in ("knowledge_version_set", "memory_version_set", "token_budget", "exclusion_summary"):
        val = data.get(field)
        if isinstance(val, str):
            data[field] = json.loads(val)
        elif val is None:
            data[field] = {} if field != "knowledge_version_set" and field != "memory_version_set" else []
    return data


def _item_from_row(row: Any) -> dict[str, Any]:
    """Map a context_pack_items row to a dict, coercing UUID and JSONB fields."""
    data = dict(row._mapping)
    # Coerce UUID fields
    for field in _UUID_FIELDS_ITEM:
        if field in data:
            data[field] = _as_uuid(data[field])
    # Coerce JSONB fields
    val = data.get("source_ref")
    if isinstance(val, str):
        data["source_ref"] = json.loads(val)
    elif val is None:
        data["source_ref"] = {}
    return data


# ═══════════════════════════════════════════════════════════════════════
# Public API — create
# ═══════════════════════════════════════════════════════════════════════

def create_context_pack(
    db: Session,
    context: RequestContext,
    *,
    agent_id: UUID | None = None,
    project_id: UUID | None = None,
    compile_mode: str = "full",
    status: str = "created",
    knowledge_version_set: list[dict] | None = None,
    memory_version_set: list[dict] | None = None,
    token_budget: dict | None = None,
    exclusion_summary: dict | None = None,
    api_call_log_id: UUID | None = None,
) -> dict[str, Any]:
    """Insert a new context_packs row."""
    pack_id = uuid4()
    row = db.execute(
        _INSERT_PACK,
        {
            "pack_id": pack_id,
            "request_id": context.request_id,
            "correlation_id": context.correlation_id,
            "agent_id": agent_id,
            "project_id": project_id,
            "actor_type": context.actor.actor_type,
            "actor_id": context.actor.actor_id,
            "compile_mode": compile_mode,
            "status": status,
            "knowledge_version_set": json.dumps(knowledge_version_set or []),
            "memory_version_set": json.dumps(memory_version_set or []),
            "token_budget": json.dumps(token_budget or {}),
            "exclusion_summary": json.dumps(exclusion_summary or {}),
            "api_call_log_id": api_call_log_id,
        },
    ).one()
    return _pack_from_row(row)


def create_context_pack_item(
    db: Session,
    *,
    pack_id: UUID,
    item_order: int,
    item_type: str,
    object_id: UUID | None = None,
    object_version: int | None = None,
    source_ref: dict | None = None,
    included: bool = True,
    exclusion_reason: str | None = None,
    score: float | None = None,
    token_count: int | None = None,
    reason: str | None = None,
    content_digest: str | None = None,
) -> dict[str, Any]:
    """Insert a new context_pack_items row."""
    item_id = uuid4()
    row = db.execute(
        _INSERT_ITEM,
        {
            "item_id": item_id,
            "pack_id": pack_id,
            "item_order": item_order,
            "item_type": item_type,
            "object_id": object_id,
            "object_version": object_version,
            "source_ref": json.dumps(source_ref or {}),
            "included": included,
            "exclusion_reason": exclusion_reason,
            "score": score,
            "token_count": token_count,
            "reason": reason,
            "content_digest": content_digest,
        },
    ).one()
    return _item_from_row(row)


def get_context_pack(db: Session, pack_id: UUID) -> dict[str, Any] | None:
    """Get a single context pack by primary key."""
    row = db.execute(_SELECT_PACK_BY_ID, {"pack_id": pack_id}).first()
    if row is None:
        return None
    return _pack_from_row(row)


def get_context_pack_items(db: Session, pack_id: UUID) -> list[dict[str, Any]]:
    """Get all items for a context pack, ordered by item_order."""
    rows = db.execute(_SELECT_ITEMS_BY_PACK, {"pack_id": pack_id}).all()
    return [_item_from_row(row) for row in rows]


def list_context_packs(
    db: Session,
    *,
    agent_id: UUID | None = None,
    project_id: UUID | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[dict[str, Any]], int]:
    """List context packs with optional filters and pagination."""
    params = {
        "agent_id": agent_id,
        "project_id": project_id,
        "status": status,
    }
    total = db.execute(_LIST_PACKS_COUNT, params).scalar_one()
    offset = (page - 1) * page_size
    rows = db.execute(
        _LIST_PACKS,
        {**params, "page_size": page_size, "offset": offset},
    ).all()
    items = [_pack_from_row(row) for row in rows]
    return items, total
