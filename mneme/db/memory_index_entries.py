"""P4-07/P5-02 Memory index entries data-access layer.

Raw-SQL CRUD against ``memory_index_entries``.  All writes are called
from within existing ``memories.py`` transactions (no independent commit).

DDL alignment
-------------
Table: ``memory_index_entries`` (baseline L905-926)

* ``embedding_model_id`` — Phase 4 NULL, linked to provider_models in Phase 5+
* ``embedding vector(1536)`` — Phase 4 NULL, vector_state='pending' (Phase 5+)
* ``fts_vector tsvector`` — GENERATED ALWAYS AS (…) STORED, auto from index_text
* UNIQUE(memory_id, memory_version, index_profile) — enforced by ON CONFLICT
* CHECK fts_state/vector_state IN ('pending','ready','stale','failed')
"""

from __future__ import annotations

import json
from uuid import UUID, uuid4

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session


# ═══════════════════════════════════════════════════════════════════════════
# SQL — INSERT
# ═══════════════════════════════════════════════════════════════════════════

def _coerce_uuid(value):
    if value is None or isinstance(value, UUID):
        return value
    return UUID(str(value))


_INSERT = text("""
    INSERT INTO memory_index_entries (
      memory_index_entry_id, memory_id, memory_version,
      project_id, index_profile, embedding_model_id,
      content_hash, index_text,
      fts_state, vector_state
    ) VALUES (
      :eid, :mid, :ver,
      :pid, :profile, :emid,
      :chash, :itext,
      :fts_state, :vstate
    )
    ON CONFLICT (memory_id, memory_version, index_profile) DO NOTHING
    RETURNING memory_index_entry_id
""").bindparams(
    bindparam("eid", type_=PG_UUID(as_uuid=True)),
    bindparam("mid", type_=PG_UUID(as_uuid=True)),
    bindparam("pid", type_=PG_UUID(as_uuid=True)),
    bindparam("emid", type_=PG_UUID(as_uuid=True)),
)


# ═══════════════════════════════════════════════════════════════════════════
# SQL — UPDATE (state transitions)
# ═══════════════════════════════════════════════════════════════════════════

_MARK_STALE = text("""
    UPDATE memory_index_entries
    SET fts_state = 'stale',
        vector_state = CASE WHEN vector_state = 'ready' THEN 'stale' ELSE vector_state END,
        stale_at = CURRENT_TIMESTAMP,
        updated_at = CURRENT_TIMESTAMP
    WHERE memory_id = :mid
      AND fts_state = 'ready'
""").bindparams(bindparam("mid", type_=PG_UUID(as_uuid=True)))

_MARK_STALE_BY_VERSION = text("""
    UPDATE memory_index_entries
    SET fts_state = 'stale',
        vector_state = CASE WHEN vector_state = 'ready' THEN 'stale' ELSE vector_state END,
        stale_at = CURRENT_TIMESTAMP,
        updated_at = CURRENT_TIMESTAMP
    WHERE memory_id = :mid
      AND memory_version = :ver
      AND fts_state = 'ready'
""").bindparams(
    bindparam("mid", type_=PG_UUID(as_uuid=True)),
)

_MARK_FAILED = text("""
    UPDATE memory_index_entries
    SET fts_state = 'failed',
        vector_state = CASE WHEN vector_state = 'pending' THEN 'failed' ELSE vector_state END,
        last_error = :lerr,
        updated_at = CURRENT_TIMESTAMP
    WHERE memory_index_entry_id = :eid
      AND fts_state IN ('pending', 'ready')
    RETURNING
      memory_index_entry_id, memory_id, memory_version,
      fts_state, vector_state, last_error,
      updated_at
""").bindparams(bindparam("eid", type_=PG_UUID(as_uuid=True)))

_REBUILD = text("""
    UPDATE memory_index_entries
    SET index_text = :itext,
        fts_state = 'ready',
        ready_at = CURRENT_TIMESTAMP,
        stale_at = NULL,
        last_error = NULL,
        updated_at = CURRENT_TIMESTAMP
    WHERE memory_index_entry_id = :eid
      AND fts_state IN ('ready', 'stale')
    RETURNING
      memory_index_entry_id, memory_id, memory_version,
      fts_state, vector_state, ready_at, stale_at
""").bindparams(bindparam("eid", type_=PG_UUID(as_uuid=True)))

_MARK_VECTOR_STALE = text("""
    UPDATE memory_index_entries
    SET vector_state = 'stale',
        stale_at = CURRENT_TIMESTAMP,
        updated_at = CURRENT_TIMESTAMP
    WHERE memory_id = :mid
      AND vector_state = 'ready'
""").bindparams(bindparam("mid", type_=PG_UUID(as_uuid=True)))


# ═══════════════════════════════════════════════════════════════════════════
# SQL — SELECT (read paths)
# ═══════════════════════════════════════════════════════════════════════════

_SELECT_COLUMNS = """
    memory_index_entry_id, memory_id, memory_version,
    project_id, index_profile, embedding_model_id,
    content_hash, index_text,
    fts_state, vector_state,
    ready_at, stale_at, last_error,
    created_at, updated_at
"""

_UPDATE_VECTOR_READY_PG = text(f"""
    UPDATE memory_index_entries
    SET embedding_model_id = :emid,
        embedding = CAST(:embedding AS vector),
        vector_state = 'ready',
        ready_at = CURRENT_TIMESTAMP,
        stale_at = NULL,
        last_error = NULL,
        updated_at = CURRENT_TIMESTAMP
    WHERE memory_index_entry_id = :eid
    RETURNING {_SELECT_COLUMNS}
""").bindparams(
    bindparam("eid", type_=PG_UUID(as_uuid=True)),
    bindparam("emid", type_=PG_UUID(as_uuid=True)),
)

_UPDATE_VECTOR_READY_GENERIC = text(f"""
    UPDATE memory_index_entries
    SET embedding_model_id = :emid,
        embedding = :embedding,
        vector_state = 'ready',
        ready_at = CURRENT_TIMESTAMP,
        stale_at = NULL,
        last_error = NULL,
        updated_at = CURRENT_TIMESTAMP
    WHERE memory_index_entry_id = :eid
    RETURNING {_SELECT_COLUMNS}
""").bindparams(
    bindparam("eid", type_=PG_UUID(as_uuid=True)),
    bindparam("emid", type_=PG_UUID(as_uuid=True)),
)

_MARK_VECTOR_FAILED = text(f"""
    UPDATE memory_index_entries
    SET vector_state = 'failed',
        last_error = :lerr,
        updated_at = CURRENT_TIMESTAMP
    WHERE memory_index_entry_id = :eid
    RETURNING {_SELECT_COLUMNS}
""").bindparams(bindparam("eid", type_=PG_UUID(as_uuid=True)))

_GET = text(
    f"SELECT {_SELECT_COLUMNS} FROM memory_index_entries WHERE memory_index_entry_id = :eid"
).bindparams(bindparam("eid", type_=PG_UUID(as_uuid=True)))

_GET_BY_KEY = text(f"""
    SELECT {_SELECT_COLUMNS}
    FROM memory_index_entries
    WHERE memory_id = :mid
      AND memory_version = :ver
      AND index_profile = :profile
""").bindparams(bindparam("mid", type_=PG_UUID(as_uuid=True)))

_LIST_COUNT = text("""
    SELECT count(*)
    FROM memory_index_entries
    WHERE (:project_id IS NULL OR project_id = :project_id)
      AND (:fts_state IS NULL OR fts_state = :fts_state)
      AND (:vector_state IS NULL OR vector_state = :vector_state)
      AND (:memory_id IS NULL OR memory_id = :memory_id)
""").bindparams(
    bindparam("project_id", type_=PG_UUID(as_uuid=True)),
    bindparam("memory_id", type_=PG_UUID(as_uuid=True)),
)

_LIST_QUERY = text(f"""
    SELECT {_SELECT_COLUMNS}
    FROM memory_index_entries
    WHERE (:project_id IS NULL OR project_id = :project_id)
      AND (:fts_state IS NULL OR fts_state = :fts_state)
      AND (:vector_state IS NULL OR vector_state = :vector_state)
      AND (:memory_id IS NULL OR memory_id = :memory_id)
    ORDER BY memory_version DESC, created_at DESC
    LIMIT :page_size OFFSET :offset
""").bindparams(
    bindparam("project_id", type_=PG_UUID(as_uuid=True)),
    bindparam("memory_id", type_=PG_UUID(as_uuid=True)),
)

_STATUS_SUMMARY = text("""
    SELECT
      COALESCE(count(*), 0) AS total_entries,
      COALESCE(SUM(CASE WHEN fts_state = 'ready' THEN 1 ELSE 0 END), 0)   AS fts_ready,
      COALESCE(SUM(CASE WHEN fts_state = 'stale' THEN 1 ELSE 0 END), 0)   AS fts_stale,
      COALESCE(SUM(CASE WHEN fts_state = 'pending' THEN 1 ELSE 0 END), 0) AS fts_pending,
      COALESCE(SUM(CASE WHEN fts_state = 'failed' THEN 1 ELSE 0 END), 0)  AS fts_failed,
      COALESCE(SUM(CASE WHEN vector_state = 'ready' THEN 1 ELSE 0 END), 0)   AS vector_ready,
      COALESCE(SUM(CASE WHEN vector_state = 'pending' THEN 1 ELSE 0 END), 0) AS vector_pending,
      COALESCE(SUM(CASE WHEN vector_state = 'stale' THEN 1 ELSE 0 END), 0)   AS vector_stale,
      COALESCE(SUM(CASE WHEN vector_state = 'failed' THEN 1 ELSE 0 END), 0)  AS vector_failed
    FROM memory_index_entries
    WHERE (:project_id IS NULL OR project_id = :project_id)
""").bindparams(bindparam("project_id", type_=PG_UUID(as_uuid=True)))


# ═══════════════════════════════════════════════════════════════════════════
# Row mapping
# ═══════════════════════════════════════════════════════════════════════════

def _row_to_dict(row) -> dict:
    """Map a SQLAlchemy row to a plain dict (Row._mapping is a Mapping)."""
    return dict(row._mapping)


# ═══════════════════════════════════════════════════════════════════════════
# Public API — create
# ═══════════════════════════════════════════════════════════════════════════

def create_index_entry(
    db: Session,
    *,
    memory_id: UUID,
    memory_version: int,
    project_id: UUID,
    index_text: str,
    content_hash: str,
    fts_state: str = "ready",
    vector_state: str = "pending",
    index_profile: str = "default",
    embedding_model_id: UUID | None = None,
) -> UUID | None:
    """Insert a new ``memory_index_entries`` row.

    Uses ``ON CONFLICT DO NOTHING`` — safe for idempotent calls.
    Returns the ``memory_index_entry_id``, or ``None`` if a duplicate exists
    for the same ``(memory_id, memory_version, index_profile)``.

    Phase 4 notes:
    * ``vector_state`` defaults to ``'pending'`` (embedding deferred to Phase 5+).
    * ``embedding_model_id`` is ``None`` (linked to provider_models in Phase 5+).
    """
    eid = uuid4()
    result = db.execute(
        _INSERT,
        {
            "eid": eid,
            "mid": _coerce_uuid(memory_id),
            "ver": memory_version,
            "pid": _coerce_uuid(project_id),
            "profile": index_profile,
            "emid": _coerce_uuid(embedding_model_id),
            "chash": content_hash,
            "itext": index_text,
            "fts_state": fts_state,
            "vstate": vector_state,
        },
    ).first()
    if result is not None:
        return result[0]
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Public API — state transitions
# ═══════════════════════════════════════════════════════════════════════════

def mark_entries_stale(db: Session, *, memory_id: UUID) -> int:
    """Mark all 'ready' entries for a memory as 'stale'.

    Used by ``index_manager.on_memory_updated`` when memory content changes.
    Returns the number of rows updated.
    """
    result = db.execute(_MARK_STALE, {"mid": _coerce_uuid(memory_id)})
    return result.rowcount


def mark_entry_by_version_stale(
    db: Session, *, memory_id: UUID, memory_version: int
) -> int:
    """Mark a specific version's entry as stale (if it is 'ready').

    Returns the number of rows updated (0 or 1).
    """
    result = db.execute(
        _MARK_STALE_BY_VERSION,
        {"mid": _coerce_uuid(memory_id), "ver": memory_version},
    )
    return result.rowcount


def mark_entry_failed(
    db: Session, *, entry_id: UUID, error: str
) -> dict | None:
    """Mark an index entry as 'failed' with an error message.

    Only transitions from 'pending' or 'ready'. Also cascades vector_state
    to 'failed' if it was 'pending'.
    """
    row = db.execute(
        _MARK_FAILED, {"eid": _coerce_uuid(entry_id), "lerr": error}
    ).first()
    if row is None:
        return None
    return _row_to_dict(row)


def rebuild_index_entry(
    db: Session, *, entry_id: UUID, index_text: str
) -> dict | None:
    """Rebuild FTS for an entry: update index_text → fts_vector auto-refreshes.

    Only works on entries in 'ready' or 'stale' state.
    Returns the updated row as a dict, or ``None`` if entry not found
    or in an invalid state.
    """
    row = db.execute(_REBUILD, {"eid": _coerce_uuid(entry_id), "itext": index_text}).first()
    if row is None:
        return None
    return _row_to_dict(row)


# ═══════════════════════════════════════════════════════════════════════════
# Public API — read
# ═══════════════════════════════════════════════════════════════════════════

def mark_entries_vector_stale(db: Session, *, memory_id: UUID) -> int:
    """Mark all ready vector embeddings for a memory as stale."""
    result = db.execute(_MARK_VECTOR_STALE, {"mid": _coerce_uuid(memory_id)})
    return result.rowcount


def update_entry_embedding(
    db: Session,
    *,
    entry_id: UUID,
    embedding: list[float],
    embedding_model_id: UUID | None = None,
) -> dict | None:
    """Persist an embedding and mark ``vector_state`` ready."""
    embedding_literal = json.dumps([float(v) for v in embedding], separators=(",", ":"))
    dialect_name = db.get_bind().dialect.name
    statement = (
        _UPDATE_VECTOR_READY_PG
        if dialect_name == "postgresql"
        else _UPDATE_VECTOR_READY_GENERIC
    )
    row = db.execute(
        statement,
        {
            "eid": _coerce_uuid(entry_id),
            "embedding": embedding_literal,
            "emid": _coerce_uuid(embedding_model_id),
        },
    ).first()
    if row is None:
        return None
    return _row_to_dict(row)


def mark_entry_vector_failed(
    db: Session, *, entry_id: UUID, error: str
) -> dict | None:
    """Mark vector embedding for an entry as failed."""
    row = db.execute(
        _MARK_VECTOR_FAILED,
        {"eid": _coerce_uuid(entry_id), "lerr": error[:1024]},
    ).first()
    if row is None:
        return None
    return _row_to_dict(row)


def get_index_entry(db: Session, entry_id: UUID) -> dict | None:
    """Get a single index entry by primary key."""
    row = db.execute(_GET, {"eid": _coerce_uuid(entry_id)}).first()
    if row is None:
        return None
    return _row_to_dict(row)


def get_index_entry_by_key(
    db: Session,
    *,
    memory_id: UUID,
    memory_version: int,
    index_profile: str = "default",
) -> dict | None:
    """Get an index entry by its natural key (memory_id, version, profile).

    Useful for idempotent lookups before insert — a non-None return means the
    entry already exists for this UNIQUE tuple.
    """
    row = db.execute(
        _GET_BY_KEY,
        {"mid": _coerce_uuid(memory_id), "ver": memory_version, "profile": index_profile},
    ).first()
    if row is None:
        return None
    return _row_to_dict(row)


def list_index_entries(
    db: Session,
    *,
    project_id: UUID | None = None,
    fts_state: str | None = None,
    vector_state: str | None = None,
    memory_id: UUID | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[dict], int]:
    """List index entries with optional filters.

    Returns ``(items, total_count)`` tuple.
    """
    params = {
        "project_id": _coerce_uuid(project_id),
        "fts_state": fts_state,
        "vector_state": vector_state,
        "memory_id": _coerce_uuid(memory_id),
    }
    total = db.execute(_LIST_COUNT, params).scalar_one()
    offset = (page - 1) * page_size
    rows = db.execute(
        _LIST_QUERY, {**params, "page_size": page_size, "offset": offset}
    ).all()
    return [_row_to_dict(r) for r in rows], total


def get_index_status_summary(
    db: Session, *, project_id: UUID | None = None
) -> dict:
    """Return aggregated index state counts (fts + vector breakdown).

    Returns a flat dict with keys: total_entries, fts_ready, fts_stale,
    fts_pending, fts_failed, vector_ready, vector_pending, vector_stale,
    vector_failed.  All values are integers; defaults to 0 when no rows exist.
    """
    row = db.execute(_STATUS_SUMMARY, {"project_id": _coerce_uuid(project_id)}).first()
    if row is None:
        return {
            "total_entries": 0,
            "fts_ready": 0, "fts_stale": 0, "fts_pending": 0, "fts_failed": 0,
            "vector_ready": 0, "vector_pending": 0, "vector_stale": 0, "vector_failed": 0,
        }
    return _row_to_dict(row)


# ═══════════════════════════════════════════════════════════════════════════
# P6 — quality / search weight updates
# ═══════════════════════════════════════════════════════════════════════════

_UPDATE_SEARCH_WEIGHT = text("""
    UPDATE memory_index_entries
    SET search_weight = :sw,
        quality_score = :qs,
        updated_at = CURRENT_TIMESTAMP
    WHERE memory_index_entry_id = :eid
    RETURNING memory_index_entry_id, memory_id, memory_version,
              search_weight, quality_score
""").bindparams(bindparam("eid", type_=PG_UUID(as_uuid=True)))


def update_entry_search_weight(
    db: Session,
    *,
    entry_id: UUID,
    search_weight: float,
    quality_score: float | None = None,
) -> dict | None:
    """Update *search_weight* and *quality_score* on a single index entry.

    Returns the updated row as a dict, or ``None`` if the entry was not found.
    """
    row = db.execute(
        _UPDATE_SEARCH_WEIGHT,
        {"eid": entry_id, "sw": search_weight, "qs": quality_score},
    ).first()
    if row is None:
        return None
    return dict(row._mapping)
