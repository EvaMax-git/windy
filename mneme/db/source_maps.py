"""Source Maps DB layer — CRUD and provenance traversal for ``source_maps`` table.

The ``source_maps`` table is the spine of Mneme's provenance system.  Each row
records a directional link from a *source* (asset, document, block, chunk,
message, etc.) to a *target* (document, block, chunk, memory, etc.) with a
``mapping_role`` that describes the nature of the relationship (citation,
derived_from, extracted_from, transformed_from, attachment).

Functions
---------
* ``create_source_map`` — Insert a new source→target mapping.
* ``get_source_map`` — Look up a single mapping by PK.
* ``list_source_maps`` — Paginated listing with optional filters.
* ``delete_source_map`` — Remove a mapping row.
* ``list_upstream`` — Find all sources that point TO a given target.
* ``list_downstream`` — Find all targets that originate FROM a given source.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session

from mneme.schemas.knowledge import SourceMapRead


# ═══════════════════════════════════════════════════════════════════════
# SQL statements
# ═══════════════════════════════════════════════════════════════════════

_INSERT_SOURCE_MAP = text(
    """
    INSERT INTO source_maps (
      source_map_id,
      project_id,
      source_type,
      source_id,
      target_type,
      target_id,
      source_asset_id,
      source_document_id,
      source_block_id,
      target_document_id,
      target_block_id,
      target_chunk_id,
      span,
      confidence,
      mapping_role
    )
    VALUES (
      gen_random_uuid(),
      :project_id,
      :source_type,
      :source_id,
      :target_type,
      :target_id,
      :source_asset_id,
      :source_document_id,
      :source_block_id,
      :target_document_id,
      :target_block_id,
      :target_chunk_id,
      CAST(:span AS jsonb),
      :confidence,
      :mapping_role
    )
    RETURNING
      source_map_id,
      project_id,
      source_type,
      source_id,
      target_type,
      target_id,
      source_asset_id,
      source_document_id,
      source_block_id,
      target_document_id,
      target_block_id,
      target_chunk_id,
      span,
      confidence,
      mapping_role,
      created_at,
      updated_at
    """
).bindparams(
    bindparam("project_id", type_=PG_UUID(as_uuid=True)),
    bindparam("source_id", type_=PG_UUID(as_uuid=True)),
    bindparam("target_id", type_=PG_UUID(as_uuid=True)),
    bindparam("source_asset_id", type_=PG_UUID(as_uuid=True)),
    bindparam("source_document_id", type_=PG_UUID(as_uuid=True)),
    bindparam("source_block_id", type_=PG_UUID(as_uuid=True)),
    bindparam("target_document_id", type_=PG_UUID(as_uuid=True)),
    bindparam("target_block_id", type_=PG_UUID(as_uuid=True)),
    bindparam("target_chunk_id", type_=PG_UUID(as_uuid=True)),
)

_SELECT_SOURCE_MAP_BY_ID = text(
    """
    SELECT
      source_map_id,
      project_id,
      source_type,
      source_id,
      target_type,
      target_id,
      source_asset_id,
      source_document_id,
      source_block_id,
      target_document_id,
      target_block_id,
      target_chunk_id,
      span,
      confidence,
      mapping_role,
      created_at,
      updated_at
    FROM source_maps
    WHERE source_map_id = :source_map_id
    """
).bindparams(bindparam("source_map_id", type_=PG_UUID(as_uuid=True)))

_LIST_SOURCE_MAPS_COUNT = text(
    """
    SELECT count(*) FROM source_maps
    WHERE (:project_id IS NULL OR project_id = :project_id)
      AND (:source_type IS NULL OR source_type = :source_type)
      AND (:target_type IS NULL OR target_type = :target_type)
      AND (:mapping_role IS NULL OR mapping_role = :mapping_role)
      AND (:source_id IS NULL OR source_id = :source_id)
      AND (:target_id IS NULL OR target_id = :target_id)
    """
).bindparams(
    bindparam("project_id", type_=PG_UUID(as_uuid=True)),
    bindparam("source_id", type_=PG_UUID(as_uuid=True)),
    bindparam("target_id", type_=PG_UUID(as_uuid=True)),
)

_LIST_SOURCE_MAPS = text(
    """
    SELECT
      source_map_id,
      project_id,
      source_type,
      source_id,
      target_type,
      target_id,
      source_asset_id,
      source_document_id,
      source_block_id,
      target_document_id,
      target_block_id,
      target_chunk_id,
      span,
      confidence,
      mapping_role,
      created_at,
      updated_at
    FROM source_maps
    WHERE (:project_id IS NULL OR project_id = :project_id)
      AND (:source_type IS NULL OR source_type = :source_type)
      AND (:target_type IS NULL OR target_type = :target_type)
      AND (:mapping_role IS NULL OR mapping_role = :mapping_role)
      AND (:source_id IS NULL OR source_id = :source_id)
      AND (:target_id IS NULL OR target_id = :target_id)
    ORDER BY created_at DESC
    LIMIT :page_size OFFSET :offset
    """
).bindparams(
    bindparam("project_id", type_=PG_UUID(as_uuid=True)),
    bindparam("source_id", type_=PG_UUID(as_uuid=True)),
    bindparam("target_id", type_=PG_UUID(as_uuid=True)),
)

_DELETE_SOURCE_MAP = text(
    """
    DELETE FROM source_maps
    WHERE source_map_id = :source_map_id
    """
).bindparams(bindparam("source_map_id", type_=PG_UUID(as_uuid=True)))

_LIST_UPSTREAM = text(
    """
    SELECT
      source_map_id,
      project_id,
      source_type,
      source_id,
      target_type,
      target_id,
      source_asset_id,
      source_document_id,
      source_block_id,
      target_document_id,
      target_block_id,
      target_chunk_id,
      span,
      confidence,
      mapping_role,
      created_at,
      updated_at
    FROM source_maps
    WHERE target_type = :target_type
      AND target_id = :target_id
      AND (:mapping_role IS NULL OR mapping_role = :mapping_role)
    ORDER BY created_at DESC
    LIMIT :page_size OFFSET :offset
    """
).bindparams(
    bindparam("target_id", type_=PG_UUID(as_uuid=True)),
)

_UPSTREAM_COUNT = text(
    """
    SELECT count(*) FROM source_maps
    WHERE target_type = :target_type
      AND target_id = :target_id
      AND (:mapping_role IS NULL OR mapping_role = :mapping_role)
    """
).bindparams(bindparam("target_id", type_=PG_UUID(as_uuid=True)))

_LIST_DOWNSTREAM = text(
    """
    SELECT
      source_map_id,
      project_id,
      source_type,
      source_id,
      target_type,
      target_id,
      source_asset_id,
      source_document_id,
      source_block_id,
      target_document_id,
      target_block_id,
      target_chunk_id,
      span,
      confidence,
      mapping_role,
      created_at,
      updated_at
    FROM source_maps
    WHERE source_type = :source_type
      AND source_id = :source_id
      AND (:mapping_role IS NULL OR mapping_role = :mapping_role)
    ORDER BY created_at DESC
    LIMIT :page_size OFFSET :offset
    """
).bindparams(
    bindparam("source_id", type_=PG_UUID(as_uuid=True)),
)

_DOWNSTREAM_COUNT = text(
    """
    SELECT count(*) FROM source_maps
    WHERE source_type = :source_type
      AND source_id = :source_id
      AND (:mapping_role IS NULL OR mapping_role = :mapping_role)
    """
).bindparams(bindparam("source_id", type_=PG_UUID(as_uuid=True)))


# ═══════════════════════════════════════════════════════════════════════
# Row mapping
# ═══════════════════════════════════════════════════════════════════════

def _source_map_from_row(row: Any) -> SourceMapRead:
    data = dict(row._mapping)
    # Ensure span is a plain dict (psycopg may return JSON as-is or string)
    if isinstance(data.get("span"), str):
        data["span"] = json.loads(data["span"])
    return SourceMapRead.model_validate(data)


# ═══════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════


def create_source_map(
    db: Session,
    *,
    project_id: UUID,
    source_type: str,
    source_id: UUID,
    target_type: str,
    target_id: UUID,
    source_asset_id: UUID | None = None,
    source_document_id: UUID | None = None,
    source_block_id: UUID | None = None,
    target_document_id: UUID | None = None,
    target_block_id: UUID | None = None,
    target_chunk_id: UUID | None = None,
    span: dict | None = None,
    confidence: float | None = None,
    mapping_role: str = "citation",
) -> SourceMapRead:
    """Insert a new source→target mapping and return the created row."""
    row = db.execute(
        _INSERT_SOURCE_MAP,
        {
            "project_id": project_id,
            "source_type": source_type,
            "source_id": source_id,
            "target_type": target_type,
            "target_id": target_id,
            "source_asset_id": source_asset_id,
            "source_document_id": source_document_id,
            "source_block_id": source_block_id,
            "target_document_id": target_document_id,
            "target_block_id": target_block_id,
            "target_chunk_id": target_chunk_id,
            "span": json.dumps(span or {}),
            "confidence": confidence,
            "mapping_role": mapping_role,
        },
    ).one()
    return _source_map_from_row(row)


def get_source_map(db: Session, source_map_id: UUID) -> SourceMapRead | None:
    """Look up a single source_map by primary key."""
    row = db.execute(
        _SELECT_SOURCE_MAP_BY_ID, {"source_map_id": source_map_id}
    ).first()
    if row is None:
        return None
    return _source_map_from_row(row)


def list_source_maps(
    db: Session,
    *,
    project_id: UUID | None = None,
    source_type: str | None = None,
    target_type: str | None = None,
    mapping_role: str | None = None,
    source_id: UUID | None = None,
    target_id: UUID | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[SourceMapRead], int]:
    """List source_maps with optional filters and pagination.

    Returns:
        Tuple of (items, total_count).
    """
    total = db.execute(
        _LIST_SOURCE_MAPS_COUNT,
        {
            "project_id": project_id,
            "source_type": source_type,
            "target_type": target_type,
            "mapping_role": mapping_role,
            "source_id": source_id,
            "target_id": target_id,
        },
    ).scalar_one()
    offset = (page - 1) * page_size
    rows = db.execute(
        _LIST_SOURCE_MAPS,
        {
            "project_id": project_id,
            "source_type": source_type,
            "target_type": target_type,
            "mapping_role": mapping_role,
            "source_id": source_id,
            "target_id": target_id,
            "page_size": page_size,
            "offset": offset,
        },
    ).all()
    items = [_source_map_from_row(row) for row in rows]
    return items, total


def delete_source_map(db: Session, source_map_id: UUID) -> bool:
    """Delete a source_map row. Returns True if deleted, False if not found."""
    row = db.execute(
        _SELECT_SOURCE_MAP_BY_ID, {"source_map_id": source_map_id}
    ).first()
    if row is None:
        return False
    db.execute(_DELETE_SOURCE_MAP, {"source_map_id": source_map_id})
    return True


def list_upstream(
    db: Session,
    *,
    target_type: str,
    target_id: UUID,
    mapping_role: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[SourceMapRead], int]:
    """Find all source_maps that point TO a given target (upstream provenance)."""
    total = db.execute(
        _UPSTREAM_COUNT,
        {
            "target_type": target_type,
            "target_id": target_id,
            "mapping_role": mapping_role,
        },
    ).scalar_one()
    offset = (page - 1) * page_size
    rows = db.execute(
        _LIST_UPSTREAM,
        {
            "target_type": target_type,
            "target_id": target_id,
            "mapping_role": mapping_role,
            "page_size": page_size,
            "offset": offset,
        },
    ).all()
    items = [_source_map_from_row(row) for row in rows]
    return items, total


def list_downstream(
    db: Session,
    *,
    source_type: str,
    source_id: UUID,
    mapping_role: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[SourceMapRead], int]:
    """Find all source_maps that originate FROM a given source (downstream derived)."""
    total = db.execute(
        _DOWNSTREAM_COUNT,
        {
            "source_type": source_type,
            "source_id": source_id,
            "mapping_role": mapping_role,
        },
    ).scalar_one()
    offset = (page - 1) * page_size
    rows = db.execute(
        _LIST_DOWNSTREAM,
        {
            "source_type": source_type,
            "source_id": source_id,
            "mapping_role": mapping_role,
            "page_size": page_size,
            "offset": offset,
        },
    ).all()
    items = [_source_map_from_row(row) for row in rows]
    return items, total
