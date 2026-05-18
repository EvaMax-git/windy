"""FTS (Full-Text Search) engine for memory index entries (P4-07).

Uses PostgreSQL ``tsvector`` / ``tsquery`` with ``simple`` text-search config.
The ``fts_vector`` column on ``memory_index_entries`` is GENERATED ALWAYS STORED,
so updating ``index_text`` automatically refreshes the tsvector.

Design
------
* **Index**: GIN on ``idx_memory_index_entries_fts`` (DDL baseline, idempotent).
* **Search**: ``plainto_tsquery('simple', :query)`` matching ``fts_vector``.
* **ILIKE fallback**: For Chinese text where ``simple`` config may under-perform.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session


# ── GIN index (idempotent) ────────────────────────────────────────────

_ENSURE_GIN_INDEX = text(
    """
    CREATE INDEX IF NOT EXISTS idx_memory_index_entries_fts
    ON memory_index_entries USING gin(fts_vector)
    """
)


def ensure_fts_index(db: Session) -> None:
    """Ensure the GIN index for FTS exists (idempotent).

    No-op on backends that don't support GIN (e.g. SQLite).
    """
    try:
        db.execute(_ENSURE_GIN_INDEX)
    except Exception:
        pass  # GIN not supported on this backend


# ── FTS Search SQL ────────────────────────────────────────────────────

_FTS_COUNT = text("""
    SELECT count(*)
    FROM memory_index_entries mie
    JOIN memories m ON m.memory_id = mie.memory_id
    WHERE mie.fts_state = 'ready'
      AND mie.fts_vector @@ plainto_tsquery('simple', :query)
      AND (:project_id IS NULL OR mie.project_id = :project_id)
      AND m.status IN ('active', 'draft')
""").bindparams(bindparam("project_id", type_=PG_UUID(as_uuid=True)))

_FTS_QUERY = text("""
    SELECT
      mie.memory_index_entry_id, mie.memory_id, mie.memory_version,
      mie.index_text, mie.fts_state, mie.vector_state,
      ts_rank(mie.fts_vector, plainto_tsquery('simple', :query)) AS rank,
      m.title, m.memory_text, m.sensitivity_level,
      m.canonical_key, m.status, m.current_version
    FROM memory_index_entries mie
    JOIN memories m ON m.memory_id = mie.memory_id
    WHERE mie.fts_state = 'ready'
      AND mie.fts_vector @@ plainto_tsquery('simple', :query)
      AND (:project_id IS NULL OR mie.project_id = :project_id)
      AND m.status IN ('active', 'draft')
    ORDER BY rank DESC
    LIMIT :page_size OFFSET :offset
""").bindparams(bindparam("project_id", type_=PG_UUID(as_uuid=True)))

_ILIKE_COUNT = text("""
    SELECT count(*)
    FROM memory_index_entries mie
    JOIN memories m ON m.memory_id = mie.memory_id
    WHERE mie.fts_state = 'ready'
      AND mie.index_text ILIKE '%' || :query || '%'
      AND (:project_id IS NULL OR mie.project_id = :project_id)
      AND m.status IN ('active', 'draft')
""").bindparams(bindparam("project_id", type_=PG_UUID(as_uuid=True)))

_ILIKE_QUERY = text("""
    SELECT
      mie.memory_index_entry_id, mie.memory_id, mie.memory_version,
      mie.index_text, mie.fts_state, mie.vector_state,
      CAST(0.1 AS float) AS rank,
      m.title, m.memory_text, m.sensitivity_level,
      m.canonical_key, m.status, m.current_version
    FROM memory_index_entries mie
    JOIN memories m ON m.memory_id = mie.memory_id
    WHERE mie.fts_state = 'ready'
      AND mie.index_text ILIKE '%' || :query || '%'
      AND (:project_id IS NULL OR mie.project_id = :project_id)
      AND m.status IN ('active', 'draft')
    ORDER BY m.updated_at DESC
    LIMIT :page_size OFFSET :offset
""").bindparams(bindparam("project_id", type_=PG_UUID(as_uuid=True)))


def search_fts(
    db: Session,
    *,
    query: str,
    project_id: UUID | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    """Full-text search memory index entries via FTS with ILIKE fallback.

    Primary: PostgreSQL FTS via ``plainto_tsquery('simple', ...)``.
    Fallback: ILIKE when FTS returns zero results (Chinese text).
    """
    params = {"query": query, "project_id": project_id}

    # Primary FTS
    total = db.execute(_FTS_COUNT, params).scalar_one()
    if total > 0:
        offset = (page - 1) * page_size
        rows = db.execute(
            _FTS_QUERY, {**params, "page_size": page_size, "offset": offset}
        ).all()
        return [dict(row._mapping) for row in rows], total

    # ILIKE fallback
    total = db.execute(_ILIKE_COUNT, params).scalar_one()
    if total > 0:
        offset = (page - 1) * page_size
        rows = db.execute(
            _ILIKE_QUERY, {**params, "page_size": page_size, "offset": offset}
        ).all()
        return [dict(row._mapping) for row in rows], total

    return [], 0
