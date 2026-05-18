"""P9 — memory_stores data-access layer.

Provides CRUD against ``memory_stores`` table. Each store can be bound to
an agent via agent_id or remain unbound (global/pool).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session

from mneme.api.context import RequestContext
from mneme.db.transactions import transaction
from mneme.schemas.memory_stores import (
    MemoryStoreCreateRequest,
    MemoryStoreRead,
    MemoryStoreType,
    MemoryStoreUpdateRequest,
)


# ── SQL ────────────────────────────────────────────────────────────────────────

_INSERT_STORE = text("""
    INSERT INTO memory_stores (
        store_id, agent_id, name, type, description
    ) VALUES (
        :store_id, :agent_id, :name, :type, :description
    )
    RETURNING
        store_id, agent_id, name, type, description,
        created_at, updated_at
""").bindparams(
    bindparam("store_id", type_=PG_UUID(as_uuid=True)),
    bindparam("agent_id", type_=PG_UUID(as_uuid=True)),
)

_SELECT_STORE = text("""
    SELECT store_id, agent_id, name, type, description,
           created_at, updated_at
    FROM memory_stores
    WHERE store_id = :store_id
""").bindparams(bindparam("store_id", type_=PG_UUID(as_uuid=True)))

_LIST_STORES = text("""
    SELECT store_id, agent_id, name, type, description,
           created_at, updated_at
    FROM memory_stores
    ORDER BY created_at DESC
""")

_LIST_BY_AGENT = text("""
    SELECT store_id, agent_id, name, type, description,
           created_at, updated_at
    FROM memory_stores
    WHERE agent_id = :agent_id
    ORDER BY created_at DESC
""").bindparams(bindparam("agent_id", type_=PG_UUID(as_uuid=True)))

_LIST_UNBOUND = text("""
    SELECT store_id, agent_id, name, type, description,
           created_at, updated_at
    FROM memory_stores
    WHERE agent_id IS NULL
    ORDER BY created_at DESC
""")

_UPDATE_STORE = text("""
    UPDATE memory_stores
    SET
        name = COALESCE(:name, name),
        type = COALESCE(:type, type),
        agent_id = CASE WHEN :agent_id_set THEN :agent_id ELSE agent_id END,
        description = COALESCE(:description, description),
        updated_at = NOW()
    WHERE store_id = :store_id
    RETURNING
        store_id, agent_id, name, type, description,
        created_at, updated_at
""").bindparams(
    bindparam("store_id", type_=PG_UUID(as_uuid=True)),
    bindparam("agent_id", type_=PG_UUID(as_uuid=True)),
)

_DELETE_STORE = text("""
    DELETE FROM memory_stores
    WHERE store_id = :store_id
    RETURNING store_id
""").bindparams(bindparam("store_id", type_=PG_UUID(as_uuid=True)))


# ── Row mapping ────────────────────────────────────────────────────────────────

def _store_from_row(row: Any) -> MemoryStoreRead:
    data = dict(row._mapping)
    return MemoryStoreRead.model_validate(data)


# ── Public API ─────────────────────────────────────────────────────────────────

def create_store(
    db: Session,
    *,
    payload: MemoryStoreCreateRequest,
    context: RequestContext | None = None,
) -> MemoryStoreRead:
    store_id = uuid4()

    with transaction(db):
        row = db.execute(
            _INSERT_STORE,
            {
                "store_id": store_id,
                "agent_id": payload.agent_id,
                "name": payload.name,
                "type": payload.type.value,
                "description": payload.description,
            },
        ).one()

    return _store_from_row(row)


def get_store(db: Session, store_id: UUID) -> MemoryStoreRead | None:
    row = db.execute(_SELECT_STORE, {"store_id": store_id}).first()
    if row is None:
        return None
    return _store_from_row(row)


def list_stores(
    db: Session,
    *,
    agent_id: UUID | None = None,
    unbound_only: bool = False,
) -> list[MemoryStoreRead]:
    if unbound_only:
        rows = db.execute(_LIST_UNBOUND).all()
    elif agent_id is not None:
        rows = db.execute(_LIST_BY_AGENT, {"agent_id": agent_id}).all()
    else:
        rows = db.execute(_LIST_STORES).all()
    return [_store_from_row(row) for row in rows]


def update_store(
    db: Session,
    *,
    store_id: UUID,
    payload: MemoryStoreUpdateRequest,
    context: RequestContext | None = None,
) -> MemoryStoreRead:
    existing = get_store(db, store_id)
    if existing is None:
        raise ValueError(f"memory_store {store_id} not found")

    fields = payload.model_fields_set
    if not fields:
        raise ValueError("store update payload is empty")

    with transaction(db):
        row = db.execute(
            _UPDATE_STORE,
            {
                "store_id": store_id,
                "name": payload.name if "name" in fields else existing.name,
                "type": payload.type.value if "type" in fields and payload.type else existing.type.value,
                "agent_id_set": "agent_id" in fields,
                "agent_id": payload.agent_id if "agent_id" in fields else existing.agent_id,
                "description": payload.description if "description" in fields else existing.description,
            },
        ).first()

        if row is None:
            raise ValueError(f"memory_store {store_id} cannot be updated")
    return _store_from_row(row)


def delete_store(
    db: Session,
    *,
    store_id: UUID,
) -> bool:
    with transaction(db):
        row = db.execute(_DELETE_STORE, {"store_id": store_id}).first()
    return row is not None


def bind_store_to_agent(
    db: Session,
    *,
    store_id: UUID,
    agent_id: UUID,
) -> MemoryStoreRead:
    """Bind a store to an agent. Raises ValueError if store not found."""
    existing = get_store(db, store_id)
    if existing is None:
        raise ValueError(f"memory_store {store_id} not found")

    with transaction(db):
        row = db.execute(
            text("""
                UPDATE memory_stores
                SET agent_id = :agent_id, updated_at = NOW()
                WHERE store_id = :store_id
                RETURNING store_id, agent_id, name, type, description, created_at, updated_at
            """),
            {"store_id": store_id, "agent_id": agent_id},
        ).first()

        if row is None:
            raise ValueError(f"memory_store {store_id} cannot be bound")
    return _store_from_row(row)


def unbind_store(db: Session, *, store_id: UUID) -> MemoryStoreRead:
    """Unbind a store from its agent. Raises ValueError if store not found."""
    existing = get_store(db, store_id)
    if existing is None:
        raise ValueError(f"memory_store {store_id} not found")

    with transaction(db):
        row = db.execute(
            text("""
                UPDATE memory_stores
                SET agent_id = NULL, updated_at = NOW()
                WHERE store_id = :store_id
                RETURNING store_id, agent_id, name, type, description, created_at, updated_at
            """),
            {"store_id": store_id},
        ).first()

        if row is None:
            raise ValueError(f"memory_store {store_id} cannot be unbound")
    return _store_from_row(row)


def get_store_by_agent(db: Session, agent_id: UUID) -> MemoryStoreRead | None:
    """Get the first store bound to a given agent."""
    rows = db.execute(_LIST_BY_AGENT, {"agent_id": agent_id}).all()
    stores = [_store_from_row(row) for row in rows]
    return stores[0] if stores else None
