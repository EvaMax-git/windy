"""P4-07 Memory Index Entries API — FTS rebuild + entry listing + status summary.

Endpoints
---------
* ``GET    /api/v4/memory/index/entries``             — list index entries (paginated, filterable)
* ``GET    /api/v4/memory/index/entries/{entry_id}``    — get single index entry
* ``POST   /api/v4/memory/index/rebuild-fts``           — rebuild FTS for a memory
* ``GET    /api/v4/memory/index/states``                — (alias) list index entries
* ``GET    /api/v4/memory/index/status``                — aggregated index state summary
"""

from __future__ import annotations

import math
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from mneme.api.context import RequestContext, get_request_context
from mneme.api.errors import ApiError
from mneme.api.schemas import envelope
from mneme.db.base import get_db
from mneme.db.memory_index_entries import (
    create_index_entry,
    get_index_entry,
    get_index_entry_by_key,
    get_index_status_summary,
    list_index_entries,
    mark_entries_stale,
    rebuild_index_entry,
)
from mneme.db.memories import get_memory
from mneme.memory.embedding import (
    EmbeddingEntryNotFound,
    EmbeddingError,
    embed_index_entry,
)
from mneme.memory.fts import ensure_fts_index
from mneme.memory.index_manager import _build_index_text, _compute_content_hash
from mneme.schemas.common import PageInfo, PaginationParams
from mneme.schemas.memory_index import (
    MemoryIndexEntryListResponse,
    MemoryIndexEntryRead,
    MemoryIndexFilterParams,
    MemoryIndexRebuildRequest,
    MemoryIndexStateSummary,
)

router = APIRouter(prefix="/memory-index", tags=["memory-index"])


def _page_info(total: int, page: int, page_size: int) -> PageInfo:
    """Build a PageInfo model for paginated responses."""
    total_pages = max(1, math.ceil(total / max(page_size, 1)))
    return PageInfo(
        page=page,
        page_size=page_size,
        total_items=total,
        total_pages=total_pages,
        has_next=page < total_pages,
        has_previous=page > 1,
    )


# ═══════════════════════════════════════════════════════════════════════
# GET /memory/index/entries — list index entries
# ═══════════════════════════════════════════════════════════════════════

@router.get("/entries", response_model=dict)
def list_index_entries_endpoint(
    pagination: PaginationParams = Depends(),
    filters: MemoryIndexFilterParams = Depends(),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """List ``memory_index_entries`` rows, filterable by project/state/memory.

    Supports pagination via ``page`` and ``page_size`` query parameters.
    Filters available: ``project_id``, ``fts_state``, ``memory_id``.
    Results are ordered by ``memory_version DESC, created_at DESC``.
    """
    items, total = list_index_entries(
        db,
        project_id=filters.project_id,
        fts_state=filters.fts_state.value if filters.fts_state else None,
        vector_state=filters.vector_state.value if filters.vector_state else None,
        memory_id=filters.memory_id,
        page=pagination.page,
        page_size=pagination.page_size,
    )

    entries = [MemoryIndexEntryRead.model_validate(item) for item in items]
    pi = _page_info(total, pagination.page, pagination.page_size)
    data = MemoryIndexEntryListResponse(items=entries, page_info=pi)

    return envelope(
        data.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════
# GET /memory/index/entries/{entry_id} — get single index entry
# ═══════════════════════════════════════════════════════════════════════

@router.get("/entries/{entry_id}", response_model=dict)
def get_index_entry_endpoint(
    entry_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Get a single ``memory_index_entries`` row by ID.

    Returns the full entry including ``index_text``, ``fts_state``,
    ``vector_state``, timestamps, and any errors.
    """
    entry = get_index_entry(db, entry_id)
    if entry is None:
        raise ApiError(
            404, "bad_request", f"memory_index_entry {entry_id} not found"
        )

    data = MemoryIndexEntryRead.model_validate(entry)
    return envelope(
        data.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════
# GET /memory/index/states — (alias) list index entries
# ═══════════════════════════════════════════════════════════════════════

@router.get("/states", response_model=dict)
def list_index_states_endpoint(
    pagination: PaginationParams = Depends(),
    filters: MemoryIndexFilterParams = Depends(),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """(Alias for ``GET /memory/index/entries``) List index entries.

    Kept for backward compatibility.  Prefer ``GET /entries`` for new code.
    """
    return list_index_entries_endpoint(
        pagination=pagination,
        filters=filters,
        db=db,
        context=context,
    )


# ═══════════════════════════════════════════════════════════════════════
# POST /memory/index/rebuild-fts — rebuild FTS for a memory
# ═══════════════════════════════════════════════════════════════════════

@router.post("/rebuild-fts", response_model=dict)
def rebuild_fts_endpoint(
    body: MemoryIndexRebuildRequest,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Rebuild the full-text-search index for a memory.

    Two modes of operation:

    1. **Entry-level rebuild** — provide ``memory_index_entry_id``.
       The existing entry's ``index_text`` is updated and its
       ``fts_vector`` column is auto-refreshed by the database.

    2. **Memory-level rebuild** — provide ``memory_id``.
       All existing 'ready' entries for the memory are marked 'stale',
       then a fresh entry is created for the current version.

    If ``index_text`` is supplied it overrides the auto-generated text
    (built from ``title + memory_text``).  Otherwise the text is
    rebuilt from the live memory row.
    """
    ensure_fts_index(db)

    # ── Entry-level rebuild ────────────────────────────────────────────

    if body.memory_index_entry_id:
        entry = get_index_entry(db, body.memory_index_entry_id)
        if entry is None:
            raise ApiError(
                404, "bad_request",
                f"memory_index_entry {body.memory_index_entry_id} not found",
            )
        index_text = body.index_text or entry["index_text"]
        result = rebuild_index_entry(
            db, entry_id=body.memory_index_entry_id, index_text=index_text,
        )
        if result is None:
            raise ApiError(
                409, "bad_request",
                f"memory_index_entry {body.memory_index_entry_id} "
                f"is not in a rebuildable state (expected 'ready' or 'stale')",
            )
        db.commit()
        return envelope(
            {"rebuilt": True, "entry": result},
            request_id=context.request_id,
            correlation_id=context.correlation_id,
        )

    # ── Memory-level rebuild ───────────────────────────────────────────

    if body.memory_id:
        mem = get_memory(db, body.memory_id)
        if mem is None:
            raise ApiError(
                404, "bad_request", f"memory {body.memory_id} not found",
            )

        if body.index_text:
            index_text = body.index_text
        else:
            index_text = _build_index_text(mem.title, mem.memory_text)

        current_entry = get_index_entry_by_key(
            db,
            memory_id=body.memory_id,
            memory_version=mem.current_version,
        )
        mark_entries_stale(db, memory_id=body.memory_id)

        if current_entry is not None:
            rebuilt_entry = rebuild_index_entry(
                db,
                entry_id=current_entry["memory_index_entry_id"],
                index_text=index_text,
            )
            eid = rebuilt_entry["memory_index_entry_id"] if rebuilt_entry else None
        else:
            content_hash = _compute_content_hash(mem.memory_text)
            eid = create_index_entry(
                db,
                memory_id=body.memory_id,
                memory_version=mem.current_version,
                project_id=mem.project_id,
                index_text=index_text,
                content_hash=content_hash,
                fts_state="ready",
            )
        db.commit()

        return envelope(
            {
                "rebuilt": True,
                "memory_id": str(body.memory_id),
                "memory_index_entry_id": str(eid) if eid else None,
            },
            request_id=context.request_id,
            correlation_id=context.correlation_id,
        )

    raise ApiError(
        400, "bad_request",
        "Either ``memory_index_entry_id`` or ``memory_id`` must be provided",
    )


# ═══════════════════════════════════════════════════════════════════════
# GET /memory/index/status — aggregated index state summary
# ═══════════════════════════════════════════════════════════════════════

@router.post("/rebuild-vector", response_model=dict)
def rebuild_vector_endpoint(
    body: MemoryIndexRebuildRequest,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Rebuild the vector embedding for a memory index entry."""
    entry_id = body.memory_index_entry_id

    if entry_id is None and body.memory_id:
        mem = get_memory(db, body.memory_id)
        if mem is None:
            raise ApiError(
                404, "bad_request", f"memory {body.memory_id} not found",
            )

        entry = get_index_entry_by_key(
            db,
            memory_id=body.memory_id,
            memory_version=mem.current_version,
        )
        if entry is None:
            index_text = body.index_text or _build_index_text(mem.title, mem.memory_text)
            content_hash = _compute_content_hash(mem.memory_text)
            entry_id = create_index_entry(
                db,
                memory_id=body.memory_id,
                memory_version=mem.current_version,
                project_id=mem.project_id,
                index_text=index_text,
                content_hash=content_hash,
                fts_state="ready",
                vector_state="pending",
            )
            if entry_id is None:
                entry = get_index_entry_by_key(
                    db,
                    memory_id=body.memory_id,
                    memory_version=mem.current_version,
                )
                entry_id = entry["memory_index_entry_id"] if entry else None
        else:
            entry_id = entry["memory_index_entry_id"]

    if entry_id is None:
        raise ApiError(
            400, "bad_request",
            "Either ``memory_index_entry_id`` or ``memory_id`` must be provided",
        )

    try:
        result = embed_index_entry(db, entry_id=entry_id, context=context)
    except EmbeddingEntryNotFound as exc:
        raise ApiError(404, "bad_request", str(exc))
    except EmbeddingError as exc:
        db.commit()
        raise ApiError(503, "dependency_unavailable", str(exc))

    db.commit()
    return envelope(
        {"rebuilt": True, **result},
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.get("/status", response_model=dict)
def get_index_status_endpoint(
    project_id: UUID | None = None,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Aggregated index state summary — counts by ``fts_state`` and
    ``vector_state``.

    Optionally scoped to a ``project_id``.  Returns zero for every
    counter when no entries exist.
    """
    summary = get_index_status_summary(db, project_id=project_id)
    data = MemoryIndexStateSummary.model_validate(summary)

    return envelope(
        data.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )
