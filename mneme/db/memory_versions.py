"""P4-06 Memory Versions data-access layer (read-only queries).

Version rows are created automatically by :mod:`mneme.db.memories` write
operations.  This module provides read-only queries for the version history.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session

from mneme.schemas.memory_versions import MemoryVersionRead

# ═══════════════════════════════════════════════════════════════════════
# SQL
# ═══════════════════════════════════════════════════════════════════════

_GET_BY_MEMORY_VERSION = text("""
    SELECT
      memory_version_id, memory_id, version, action,
      before_json, after_json,
      actor_type, actor_id,
      review_item_id, candidate_id, event_id,
      reason, created_at
    FROM memory_versions
    WHERE memory_id = :mid AND version = :ver
""").bindparams(
    bindparam("mid", type_=PG_UUID(as_uuid=True)),
)

_LIST_COUNT = text("""
    SELECT count(*) FROM memory_versions
    WHERE memory_id = :mid
      AND (:action IS NULL OR action = :action)
""").bindparams(bindparam("mid", type_=PG_UUID(as_uuid=True)))

_LIST_QUERY = text("""
    SELECT
      memory_version_id, memory_id, version, action,
      before_json, after_json,
      actor_type, actor_id,
      review_item_id, candidate_id, event_id,
      reason, created_at
    FROM memory_versions
    WHERE memory_id = :mid
      AND (:action IS NULL OR action = :action)
    ORDER BY version DESC
    LIMIT :page_size OFFSET :offset
""").bindparams(bindparam("mid", type_=PG_UUID(as_uuid=True)))


# ═══════════════════════════════════════════════════════════════════════
# Row mapping
# ═══════════════════════════════════════════════════════════════════════

def _version_from_row(row: Any) -> MemoryVersionRead:
    """Map a SQL row to MemoryVersionRead, normalizing JSONB fields."""
    data = dict(row._mapping)
    # JSONB columns arrive as strings from SQLite; parse them into dicts.
    for field in ("before_json", "after_json"):
        val = data.get(field)
        if isinstance(val, str):
            data[field] = json.loads(val) if val else {}
    return MemoryVersionRead.model_validate(data)


# ═══════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════

def list_memory_versions(
    db: Session,
    *,
    memory_id: UUID,
    action: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[MemoryVersionRead], int]:
    """List version history for a memory, newest first."""
    params = {"mid": memory_id, "action": action}
    total = db.execute(_LIST_COUNT, params).scalar_one()
    offset = (page - 1) * page_size
    rows = db.execute(
        _LIST_QUERY,
        {**params, "page_size": page_size, "offset": offset},
    ).all()
    items = [_version_from_row(row) for row in rows]
    return items, total


def get_memory_version(
    db: Session,
    *,
    memory_id: UUID,
    version: int,
) -> MemoryVersionRead | None:
    """Look up a specific version of a memory."""
    row = db.execute(
        _GET_BY_MEMORY_VERSION,
        {"mid": memory_id, "ver": version},
    ).first()
    if row is None:
        return None
    return _version_from_row(row)
