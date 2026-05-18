"""Sub-library registry data-access layer.

Every knowledge-base backend (vector stores, graph stores, full-text indexes,
custom backends) registers itself in ``sub_library_registry`` so that the
system can discover available backends and route operations accordingly.

Provides CRUD + a bootstrap helper that seeds three default sub-libraries
(default vector / default graph / default full-text).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Session

from mneme.db.transactions import session_scope, transaction
from mneme.schemas.common import PageInfo
from mneme.schemas.sub_libraries import (
    SubLibraryCreateRequest,
    SubLibraryRead,
    SubLibraryListResponse,
    SubLibraryType,
)


# ── SQL ────────────────────────────────────────────────────────────────────────

_INSERT_LIBRARY = text("""
    INSERT INTO sub_library_registry (
        id, name, type, key, capability_json, metadata_json
    ) VALUES (
        :id, :name, :type, :key, :capability_json, :metadata_json
    )
    RETURNING id, name, type, key, capability_json, metadata_json, created_at
""").bindparams(
    bindparam("id", type_=PG_UUID(as_uuid=True)),
)

_SELECT_ALL = text("""
    SELECT id, name, type, key, capability_json, metadata_json, created_at
    FROM sub_library_registry
    ORDER BY created_at ASC
""")

_SELECT_BY_ID = text("""
    SELECT id, name, type, key, capability_json, metadata_json, created_at
    FROM sub_library_registry
    WHERE id = :id
""").bindparams(bindparam("id", type_=PG_UUID(as_uuid=True)))

_SELECT_BY_KEY = text("""
    SELECT id, name, type, key, capability_json, metadata_json, created_at
    FROM sub_library_registry
    WHERE key = :key
""")

_SELECT_BY_TYPE = text("""
    SELECT id, name, type, key, capability_json, metadata_json, created_at
    FROM sub_library_registry
    WHERE type = :type
    ORDER BY created_at ASC
""")

_SELECT_COUNT = text("""
    SELECT count(*) FROM sub_library_registry
""")

_SELECT_COUNT_BY_TYPE = text("""
    SELECT count(*) FROM sub_library_registry WHERE type = :type
""")

_SELECT_ALL_PAGINATED = text("""
    SELECT id, name, type, key, capability_json, metadata_json, created_at
    FROM sub_library_registry
    ORDER BY created_at ASC
    LIMIT :limit OFFSET :offset
""")

_SELECT_BY_TYPE_PAGINATED = text("""
    SELECT id, name, type, key, capability_json, metadata_json, created_at
    FROM sub_library_registry
    WHERE type = :type
    ORDER BY created_at ASC
    LIMIT :limit OFFSET :offset
""")

_DELETE_BY_ID = text("""
    DELETE FROM sub_library_registry
    WHERE id = :id
    RETURNING id
""").bindparams(bindparam("id", type_=PG_UUID(as_uuid=True)))

_UPDATE_BY_ID = text("""
    UPDATE sub_library_registry
    SET name = COALESCE(:name, name),
        type = COALESCE(:type, type),
        key = COALESCE(:key, key),
        capability_json = COALESCE(:capability_json, capability_json),
        metadata_json = COALESCE(:metadata_json, metadata_json)
    WHERE id = :id
    RETURNING id, name, type, key, capability_json, metadata_json, created_at
""").bindparams(bindparam("id", type_=PG_UUID(as_uuid=True)))


# ── Row mapping ────────────────────────────────────────────────────────────────

def _library_from_row(row: Any) -> SubLibraryRead:
    data = dict(row._mapping)
    # Parse jsonb fields from string if needed
    for field in ("capability_json", "metadata_json"):
        val = data.get(field)
        if isinstance(val, str):
            try:
                import json as _json
                data[field] = _json.loads(val)
            except Exception:
                data[field] = {}
        elif val is None:
            data[field] = {}
    if data.get("created_at"):
        data["created_at"] = str(data["created_at"])
    if data.get("key") is None:
        data["key"] = ""
    return SubLibraryRead.model_validate(data)


# ── Public API ─────────────────────────────────────────────────────────────────

def create_sub_library(
    db: Session,
    *,
    payload: SubLibraryCreateRequest,
) -> SubLibraryRead:
    """Register a new sub-library backend."""
    lib_id = uuid4()

    import json as _json

    with transaction(db):
        row = db.execute(
            _INSERT_LIBRARY,
            {
                "id": lib_id,
                "name": payload.name,
                "type": payload.type,
                "key": payload.key or None,
                "capability_json": _json.dumps(payload.capability_json or {}),
                "metadata_json": _json.dumps(payload.metadata_json or {}),
            },
        ).one()

    return _library_from_row(row)


def get_sub_library(db: Session, lib_id: UUID) -> SubLibraryRead | None:
    """Look up a single sub-library by id."""
    row = db.execute(_SELECT_BY_ID, {"id": lib_id}).first()
    if row is None:
        return None
    return _library_from_row(row)


def get_sub_library_by_key(db: Session, key: str) -> SubLibraryRead | None:
    """Look up a sub-library by its frontend-friendly key."""
    row = db.execute(_SELECT_BY_KEY, {"key": key}).first()
    if row is None:
        return None
    return _library_from_row(row)


def list_sub_libraries(
    db: Session,
    *,
    type_filter: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> SubLibraryListResponse:
    """List registered sub-libraries with optional type filter and pagination.

    Returns a ``SubLibraryListResponse`` with ``items`` and ``page_info``.
    """
    from math import ceil

    if type_filter:
        total = db.execute(_SELECT_COUNT_BY_TYPE, {"type": type_filter}).scalar_one()
        rows = db.execute(
            _SELECT_BY_TYPE_PAGINATED,
            {"type": type_filter, "limit": page_size, "offset": (page - 1) * page_size},
        ).all()
    else:
        total = db.execute(_SELECT_COUNT).scalar_one()
        rows = db.execute(
            _SELECT_ALL_PAGINATED,
            {"limit": page_size, "offset": (page - 1) * page_size},
        ).all()

    items = [_library_from_row(row) for row in rows]
    total_pages = max(1, ceil(total / max(page_size, 1)))
    page_info = PageInfo(
        page=page,
        page_size=page_size,
        total_items=total,
        total_pages=total_pages,
        has_next=page < total_pages,
        has_previous=page > 1,
    )
    return SubLibraryListResponse(items=items, page_info=page_info)


def update_sub_library(
    db: Session,
    lib_id: UUID,
    *,
    name: str | None = None,
    type: str | None = None,
    key: str | None = None,
    capability_json: dict | None = None,
    metadata_json: dict | None = None,
) -> SubLibraryRead | None:
    """Update a sub-library registration. Only non-None fields are applied."""
    import json as _json

    row = db.execute(
        _UPDATE_BY_ID,
        {
            "id": lib_id,
            "name": name,
            "type": type,
            "key": key,
            "capability_json": _json.dumps(capability_json) if capability_json is not None else None,
            "metadata_json": _json.dumps(metadata_json) if metadata_json is not None else None,
        },
    ).first()

    if row is None:
        return None

    return _library_from_row(row)


def delete_sub_library(db: Session, *, lib_id: UUID) -> bool:
    """Remove a sub-library registration. Returns True if deleted, False if not found."""
    with transaction(db):
        row = db.execute(_DELETE_BY_ID, {"id": lib_id}).first()
    return row is not None


def delete_sub_library_by_key(db: Session, key: str) -> SubLibraryRead | None:
    """Delete a sub-library by its key. Returns the deleted record or None."""
    existing = get_sub_library_by_key(db, key)
    if existing is None:
        return None
    deleted = delete_sub_library(db, lib_id=existing.id)
    return existing if deleted else None


# ── Bootstrap / Seed ───────────────────────────────────────────────────────────

_DEFAULT_LIBRARIES = [
    {
        "name": "默认向量库",
        "type": "vector",
        "key": "vector",
        "capability_json": {"accept_chunks": ["text", "code"], "search": "cosine", "normalize": "minmax"},
    },
    {
        "name": "默认图谱库",
        "type": "graph",
        "key": "graph",
        "capability_json": {"accept_chunks": ["text", "image_caption"], "search": "graph_traversal", "normalize": "rank"},
    },
    {
        "name": "默认全文索引",
        "type": "fulltext",
        "key": "fulltext",
        "capability_json": {"accept_chunks": ["text", "code", "table"], "search": "bm25", "normalize": "tfidf"},
    },
]

_BOOTSTRAP_INSERT = text("""
    INSERT INTO sub_library_registry (id, name, type, key, capability_json)
    VALUES (gen_random_uuid(), :name, :type, :key, :capability_json)
""").bindparams(
    bindparam("capability_json", type_=JSONB),
)


def bootstrap_sub_libraries() -> int:
    """Insert the three default sub-libraries if they don't exist yet.

    Also repairs missing keys on existing rows that were seeded before
    the ``key`` column was added.

    Returns the number of rows inserted or repaired.
    """
    with session_scope() as db:
        # ── Repair legacy rows (seeded without key) ────────────────────────
        repair_count = 0
        legacy_rows = db.execute(
            text("""
                SELECT id, type FROM sub_library_registry
                WHERE (key IS NULL OR key = '')
                  AND type IN ('vector', 'graph', 'fulltext')
            """),
        ).all()

        import json as _json

        _REPAIR_BY_TYPE = {
            "vector":   {"key": "vector",   "capability_json": _json.dumps({"accept_chunks": ["text", "code"], "search": "cosine", "normalize": "minmax"})},
            "graph":    {"key": "graph",    "capability_json": _json.dumps({"accept_chunks": ["text", "image_caption"], "search": "graph_traversal", "normalize": "rank"})},
            "fulltext": {"key": "fulltext", "capability_json": _json.dumps({"accept_chunks": ["text", "code", "table"], "search": "bm25", "normalize": "tfidf"})},
        }

        for row in legacy_rows:
            row_type = row._mapping["type"]
            repairs = _REPAIR_BY_TYPE.get(row_type)
            if repairs is None:
                continue
            db.execute(
                text("""
                    UPDATE sub_library_registry
                    SET key = :key,
                        capability_json = :capability_json
                    WHERE id = :id
                """),
                {
                    "id": row._mapping["id"],
                    "key": repairs["key"],
                    "capability_json": repairs["capability_json"],
                },
            )
            repair_count += 1

        # ── Insert each default if not already present (by type) ───────────
        inserted = 0
        for lib in _DEFAULT_LIBRARIES:
            existing = db.execute(
                text("SELECT 1 FROM sub_library_registry WHERE type = :type"),
                {"type": lib["type"]},
            ).first()
            if existing is not None:
                continue
            db.execute(
                _BOOTSTRAP_INSERT,
                {
                    "name": lib["name"],
                    "type": lib["type"],
                    "key": lib["key"],
                    "capability_json": lib["capability_json"],
                },
            )
            inserted += 1

    return inserted + repair_count
