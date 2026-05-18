"""FTS (Full-Text Search) engine for knowledge chunks (P3-07).

Uses PostgreSQL ``tsvector`` / ``tsquery`` with the ``simple`` text-search
config (no stemming — works well for both Chinese and English).

Design
------
* **Index**: GIN on ``to_tsvector('simple', chunk_text)`` on ``knowledge_chunks``.
* **Search**: ``plainto_tsquery('simple', :query)`` for user-friendly matching.
* **Ranking**: ``ts_rank()`` weighted by match quality.
* **index_states**: Tracks ``fts_state`` per document (pending → stale → ready).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session

from mneme.schemas.knowledge import KnowledgeFtsSearchResult
from mneme.knowledge.jieba_segment import segment_query, is_available as jieba_available


# ═══════════════════════════════════════════════════════════════════
# DDL — GIN index (idempotent)
# ═══════════════════════════════════════════════════════════════════

_CREATE_FTS_INDEX = text(
    """
    CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_fts
    ON knowledge_chunks
    USING gin(to_tsvector('simple', chunk_text))
    """
)


def ensure_fts_index(db: Session) -> None:
    """Create the GIN expression index for FTS if it does not already exist.

    Safe to call at startup or before any search.  Uses ``IF NOT EXISTS``
    so repeated calls are no-ops.
    """
    db.execute(_CREATE_FTS_INDEX)
    db.commit()


# ═══════════════════════════════════════════════════════════════════
# Search SQL
# ═══════════════════════════════════════════════════════════════════

_SEARCH_CHUNKS_COUNT = text(
    """
    SELECT count(*)
    FROM knowledge_chunks kc
    JOIN knowledge_documents kd ON kd.document_id = kc.document_id
    WHERE to_tsvector('simple', kc.chunk_text) @@
          (plainto_tsquery('simple', :query) ||
           plainto_tsquery('simple', regexp_replace(:query, '[-_]', ' ', 'g')))
      AND (:project_id IS NULL OR kd.project_id = :project_id)
      AND kd.document_status = 'active'
      AND (:sensitivity_floor IS NULL
           OR array_position(ARRAY['public','normal','private','sensitive','secret'],
                             kd.sensitivity_level) >= :sensitivity_floor)
    """
).bindparams(bindparam("project_id", type_=PG_UUID(as_uuid=True)))

_SEARCH_CHUNKS = text(
    """
    SELECT
      kc.chunk_id,
      kc.document_id,
      kc.block_id,
      kc.chunk_order,
      kc.chunk_text,
      ts_rank(to_tsvector('simple', kc.chunk_text),
              plainto_tsquery('simple', :query) ||
              plainto_tsquery('simple', regexp_replace(:query, '[-_]', ' ', 'g'))) AS rank,
      kd.title AS document_title,
      kd.canonical_uri AS document_uri,
      kd.sensitivity_level AS document_sensitivity,
      kb.block_key,
      kb.block_type,
      kb.block_order
    FROM knowledge_chunks kc
    JOIN knowledge_documents kd ON kd.document_id = kc.document_id
    LEFT JOIN knowledge_blocks kb ON kb.block_id = kc.block_id
    WHERE to_tsvector('simple', kc.chunk_text) @@
          (plainto_tsquery('simple', :query) ||
           plainto_tsquery('simple', regexp_replace(:query, '[-_]', ' ', 'g')))
      AND (:project_id IS NULL OR kd.project_id = :project_id)
      AND kd.document_status = 'active'
      AND (:sensitivity_floor IS NULL
           OR array_position(ARRAY['public','normal','private','sensitive','secret'],
                             kd.sensitivity_level) >= :sensitivity_floor)
    ORDER BY rank DESC
    LIMIT :page_size OFFSET :offset
    """
).bindparams(bindparam("project_id", type_=PG_UUID(as_uuid=True)))


# ═══════════════════════════════════════════════════════════════════
# Public API — search
# ═══════════════════════════════════════════════════════════════════

_SENSITIVITY_ORDER = ["public", "normal", "private", "sensitive", "secret"]


def search_fts(
    db: Session,
    *,
    query: str,
    project_id: UUID | None = None,
    sensitivity_floor: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[KnowledgeFtsSearchResult], int]:
    """Full-text search across knowledge chunks.

    Args:
        db: Active SQLAlchemy session.
        query: User-supplied search query (plain text, supports multiple words).
        project_id: Optional project filter.
        sensitivity_floor: Minimum sensitivity level (e.g. ``'normal'``).
            Uses array_position ordinal comparison in SQL.
        page: 1-based page number.
        page_size: Results per page (max 100).

    Returns:
        Tuple of ``(results, total_count)``.
    """
    # Segment query with jieba for CJK support (fallback: raw query)
    search_query = segment_query(query) if jieba_available() else query

    # Normalise sensitivity floor to an ordinal position (1-based for array_position)
    sensitivity_ordinal: int | None = None
    if sensitivity_floor is not None and sensitivity_floor in _SENSITIVITY_ORDER:
        sensitivity_ordinal = _SENSITIVITY_ORDER.index(sensitivity_floor) + 1

    total = db.execute(
        _SEARCH_CHUNKS_COUNT,
        {
            "query": search_query,
            "project_id": project_id,
            "sensitivity_floor": sensitivity_ordinal,
        },
    ).scalar_one()

    offset = (page - 1) * page_size
    rows = db.execute(
        _SEARCH_CHUNKS,
        {
            "query": search_query,
            "project_id": project_id,
            "sensitivity_floor": sensitivity_ordinal,
            "page_size": page_size,
            "offset": offset,
        },
    ).all()

    results: list[KnowledgeFtsSearchResult] = []
    for row in rows:
        data = dict(row._mapping)
        results.append(KnowledgeFtsSearchResult.model_validate(data))

    return results, total


# ═══════════════════════════════════════════════════════════════════
# Index state helpers (thin wrappers over DB layer SQL)
# ═══════════════════════════════════════════════════════════════════

from mneme.db.knowledge import (  # noqa: E402
    _init_index_state,
    _mark_fts_failed,
    _mark_fts_ready,
    _read_index_state,
    _select_stale_fts_indexes,
)


def init_index_state(db: Session, *, document_id: UUID) -> None:
    """Create an initial ``index_states`` row for a document if missing.

    Sets ``fts_state = 'pending'`` and ``citation_state = 'pending'``.
    Safe to call on every document creation (uses ON CONFLICT DO NOTHING).
    """
    _init_index_state(db, document_id)


def refresh_stale_fts_indexes(db: Session) -> int:
    """Find documents with ``fts_state = 'stale'`` and mark them ready.

    Since the actual FTS index is a live GIN expression index on
    ``knowledge_chunks.chunk_text``, a "rebuild" simply means updating
    the ``index_states`` row to reflect that the GIN index is current.

    Returns:
        Number of documents whose FTS state was moved from stale to ready.
    """
    stale_ids = _select_stale_fts_indexes(db)
    count = 0
    for document_id in stale_ids:
        _mark_fts_ready(db, document_id)
        count += 1
    return count


def get_index_state(db: Session, *, document_id: UUID) -> dict[str, Any] | None:
    """Read the ``index_states`` row for a document."""
    return _read_index_state(db, document_id)
