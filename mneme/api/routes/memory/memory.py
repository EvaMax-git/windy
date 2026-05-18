"""P4-05 / P4-06 Memory API — CRUD + lifecycle + versions + sources.

Endpoints (P4-05)
-----------------
* ``POST   /api/v4/memory``                     — create memory (manual, draft)
* ``POST   /api/v4/memory/activate``            — activate memory from candidate
* ``GET    /api/v4/memory``                     — list (paginated, filterable)
* ``GET    /api/v4/memory/{memory_id}``         — get detail
* ``PATCH  /api/v4/memory/{memory_id}``         — update content
* ``POST   /api/v4/memory/{memory_id}/merge``   — merge into another memory
* ``POST   /api/v4/memory/{memory_id}/expire``  — expire (active → expired)
* ``POST   /api/v4/memory/{memory_id}/restore`` — restore (expired|deleted → active)
* ``DELETE /api/v4/memory/{memory_id}``         — soft-delete

Endpoints (P4-06)
-----------------
* ``GET    /api/v4/memory/{memory_id}/versions``        — version history
* ``GET    /api/v4/memory/{memory_id}/versions/{v}``     — specific version
* ``POST   /api/v4/memory/{memory_id}/sources``          — add source link
* ``GET    /api/v4/memory/{memory_id}/sources``          — list sources
* ``DELETE /api/v4/memory/sources/{memory_source_id}``   — remove source link
"""

from __future__ import annotations

import logging
import math
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mneme.api.auth import extract_agent_token
from mneme.api.context import RequestContext, get_request_context
from mneme.api.errors import ApiError
from mneme.api.schemas import envelope
from mneme.db.base import get_db
from mneme.db.memories import (
    activate_memory,
    batch_approve_memories,
    batch_reject_memories,
    create_memory,
    delete_memory,
    expire_memory,
    get_memory,
    list_memories,
    merge_memory,
    restore_memory,
    update_memory,
)
from mneme.api.dependencies.store_access import (
    check_store_ownership,
    check_memory_store_ownership,
)
from mneme.schemas.common import PageInfo, PaginationParams
from mneme.schemas.memories import (
    BatchOperationResult,
    DecayStateTransition,
    DecayStatusResponse,
    EmotionInferRequest,
    EmotionStatusResponse,
    MemoryActivate,
    MemoryBatchApproveRequest,
    MemoryBatchRejectRequest,
    MemoryCreate,
    MemoryFilterParams,
    MemoryListResponse,
    MemoryMerge,
    MemoryRead,
    MemoryStatusUpdate,
    MemoryUpdate,
    ReinforceRequest,
)
from mneme.schemas.memory_versions import (
    MemoryVersionFilterParams,
    MemoryVersionListResponse,
    MemoryVersionRead,
)
from mneme.schemas.memory_sources import (
    MemorySourceCreate,
    MemorySourceRead,
)
from mneme.schemas.memory_candidates import (
    MemoryExtractRequest,
)
from mneme.schemas.memory_index import (
    MemorySearchParams,
    MemorySearchResponse,
    MemorySearchResultItem,
    MemoryIndexStateSummary,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/memory", tags=["memory"])

# ── Agent helpers ─────────────────────────────────────────────────────────


def _maybe_agent(request: Request, db: Session):
    """Return the authenticated agent if the request uses a Bearer token."""
    token = extract_agent_token(request)
    if token is None:
        return None
    from mneme.db.agents import authenticate_agent_token
    return authenticate_agent_token(db, token)


def _require_store_access(store_id: UUID | None, agent, db: Session) -> None:
    """Verify *agent* owns *store_id*. If store_id is None, skip."""
    if store_id is None or agent is None:
        return
    check_store_ownership(store_id, agent, db)


def _page_info(total: int, page: int, page_size: int) -> PageInfo:
    total_pages = max(1, math.ceil(total / max(page_size, 1)))
    return PageInfo(
        page=page,
        page_size=page_size,
        total_items=total,
        total_pages=total_pages,
        has_next=page < total_pages,
        has_previous=page > 1,
    )


# ──────────────────────────────────────────────────────────────────────
# POST /memory
# ──────────────────────────────────────────────────────────────────────

@router.post("", response_model=dict, status_code=201)
def create_memory_endpoint(
    body: MemoryCreate,
    request: Request,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Create a memory manually (status='draft').

    The ``canonical_key`` is auto-generated as ``{project_code}-mem-{N}``
    unless explicitly provided.
    """
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "Idempotency-Key header required")

    # Agent isolation: verify agent owns the target store
    agent = _maybe_agent(request, db)
    _require_store_access(body.store_id, agent, db)

    try:
        result = create_memory(db, context, payload=body)
    except ValueError as e:
        raise ApiError(400, "bad_request", str(e))
    except IntegrityError:
        raise ApiError(
            409,
            "idempotency_conflict",
            "A memory with this canonical_key already exists in this project",
        )

    return envelope(
        result.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ──────────────────────────────────────────────────────────────────────
# POST /memory/activate
# ──────────────────────────────────────────────────────────────────────

@router.post("/activate", response_model=dict, status_code=201)
def activate_memory_endpoint(
    body: MemoryActivate,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Activate a memory from an approved candidate.

    Creates a ``memories`` row with status='active', a ``memory_versions``
    row (v1, create), and a ``memory_sources`` row (origin candidate).
    The CHECK constraint guarantees ``activated_by_review_item_id`` is set.
    """
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "Idempotency-Key header required")

    try:
        result = activate_memory(
            db,
            context,
            candidate_id=body.candidate_id,
            project_id=body.project_id,
            title=body.title,
            memory_text=body.memory_text,
            sensitivity_level=body.sensitivity_level,
            review_item_id=body.review_item_id,
            node_type=body.node_type.value if body.node_type else None,
        )
    except ValueError as e:
        raise ApiError(400, "bad_request", str(e))
    except IntegrityError:
        raise ApiError(
            409,
            "idempotency_conflict",
            "A memory with this canonical_key already exists in this project",
        )

    return envelope(
        result.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ──────────────────────────────────────────────────────────────────────
# GET /memory
# ──────────────────────────────────────────────────────────────────────

@router.get("", response_model=dict)
def list_memories_endpoint(
    pagination: PaginationParams = Depends(),
    filters: MemoryFilterParams = Depends(),
    request: Request = None,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """List memories with optional filters and pagination."""
    # Agent isolation: collect agent's store IDs for post-filtering
    agent = _maybe_agent(request, db)
    agent_store_ids: list[UUID] = []
    if agent is not None:
        from mneme.db.memory_stores import list_stores as _list_agent_stores
        agent_stores = _list_agent_stores(db, agent_id=agent.agent.agent_id)
        agent_store_ids = [s.store_id for s in agent_stores]
        if agent_store_ids:
            # If a specific store_id filter is requested, it must be in the agent's stores
            if filters.store_id is not None and filters.store_id not in agent_store_ids:
                raise ApiError(
                    403, "permission_denied",
                    f"Agent '{agent.agent.agent_code}' does not have access to store '{filters.store_id}'",
                )

    rows, total = list_memories(
        db,
        project_id=filters.project_id,
        store_id=filters.store_id,
        status=filters.status.value if filters.status else None,
        sensitivity_level=filters.sensitivity_level,
        search=filters.search,
        node_type=filters.node_type.value if filters.node_type else None,
        page=pagination.page,
        page_size=pagination.page_size,
    )

    items = [
        MemoryRead.model_validate(r.model_dump(mode="json"))
        for r in rows
    ]
    # Agent isolation: post-filter to only show memories from agent's stores
    if agent_store_ids:
        items = [it for it in items if it.store_id is None or it.store_id in agent_store_ids]
        total = len(items)

    pi = _page_info(total, pagination.page, pagination.page_size)
    data = MemoryListResponse(items=items, page_info=pi)

    return envelope(
        data.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════
# P4-10: Memory Search API
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/search", response_model=dict)
def search_memories_endpoint(
    params: MemorySearchParams = Depends(),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Hybrid search across memory index entries.

    Uses vector + FTS when vectors are ready. If vectors are unavailable or the
    query embedding cannot be produced, it degrades to FTS and reports that in
    the response.
    """
    from mneme.memory.search import search_memories

    output = search_memories(
        db,
        query=params.q,
        project_id=params.project_id,
        store_id=params.store_id,
        page=params.page,
        page_size=params.page_size,
        mode=params.mode,
        context=context,
    )

    items = []
    for row in output.rows:
        try:
            items.append(MemorySearchResultItem(
                memory_index_entry_id=row["memory_index_entry_id"],
                memory_id=row["memory_id"],
                memory_version=row["memory_version"],
                index_text=row.get("index_text", ""),
                fts_state=row.get("fts_state", "ready"),
                vector_state=row.get("vector_state", "pending"),
                rank=float(row.get("rank", 0.0)),
                fts_rank=float(row.get("fts_rank", 0.0)),
                vector_rank=float(row.get("vector_rank", 0.0)),
                search_mode=row.get("search_mode", output.search_mode),
                degraded=bool(row.get("degraded", output.degraded)),
                degradation_reason=row.get("degradation_reason", output.degradation_reason),
                stale=bool(row.get("stale", False)),
                stale_reason=row.get("stale_reason"),
                title=row.get("title"),
                memory_text=row.get("memory_text", ""),
                sensitivity_level=row.get("sensitivity_level", "private"),
                canonical_key=row.get("canonical_key", ""),
                status=row.get("status", "active"),
                current_version=int(row.get("current_version", 1)),
            ))
        except Exception:
            continue

    pi = _page_info(output.total, params.page, params.page_size)
    data = MemorySearchResponse(
        items=items,
        page_info=pi,
        search_mode=output.search_mode,
        degraded=output.degraded,
        degradation_reason=output.degradation_reason,
        stale_count=output.stale_count,
        rerank_applied=output.rerank_applied,
    )

    return envelope(
        data.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.get("/search/status", response_model=dict)
def search_status_endpoint(
    project_id: UUID | None = None,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Aggregated index state summary for search.

    Returns counts by ``fts_state`` and ``vector_state`` so the frontend
    can display how many index entries are ready / stale / pending / failed.
    """
    from mneme.db.memory_index_entries import get_index_status_summary

    summary = get_index_status_summary(db, project_id=project_id)
    data = MemoryIndexStateSummary.model_validate(summary)

    return envelope(
        data.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ──────────────────────────────────────────────────────────────────────
# GET /memory/{memory_id}
# ──────────────────────────────────────────────────────────────────────

@router.get("/{memory_id}", response_model=dict)
def get_memory_endpoint(
    memory_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Get a single memory by ID."""
    row = get_memory(db, memory_id)
    if row is None:
        raise ApiError(404, "bad_request", f"memory {memory_id} not found")

    return envelope(
        row.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ──────────────────────────────────────────────────────────────────────
# PATCH /memory/{memory_id}
# ──────────────────────────────────────────────────────────────────────

@router.patch("/{memory_id}", response_model=dict)
def update_memory_endpoint(
    memory_id: UUID,
    body: MemoryUpdate,
    request: Request,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Update mutable fields: title, memory_text, sensitivity_level.

    Increments ``current_version`` and records a ``memory_versions`` row.
    """
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "Idempotency-Key header required")

    existing = get_memory(db, memory_id)
    if existing is None:
        raise ApiError(404, "bad_request", f"memory {memory_id} not found")

    # Agent isolation: if changing store_id, verify agent owns the new store
    agent = _maybe_agent(request, db)
    if body.store_id is not None:
        _require_store_access(body.store_id, agent, db)

    try:
        result = update_memory(db, context, memory_id=memory_id, payload=body)
    except ValueError as e:
        raise ApiError(409, "bad_request", str(e))

    return envelope(
        result.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ──────────────────────────────────────────────────────────────────────
# POST /memory/{memory_id}/merge
# ──────────────────────────────────────────────────────────────────────

@router.post("/{memory_id}/merge", response_model=dict)
def merge_memory_endpoint(
    memory_id: UUID,
    body: MemoryMerge,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Merge ``target_memory_id`` into this memory.

    This memory (survivor) absorbs ``target_memory_id`` (consumed).
    - Consumed memory's text is appended to the survivor.
    - Consumed memory status → 'merged'.
    - A ``memory_relations`` row (merged_into) is created: consumed → survivor.
    """
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "Idempotency-Key header required")

    existing = get_memory(db, memory_id)
    if existing is None:
        raise ApiError(404, "bad_request", f"memory {memory_id} not found")

    target = get_memory(db, body.target_memory_id)
    if target is None:
        raise ApiError(
            404, "bad_request", f"target memory {body.target_memory_id} not found"
        )

    try:
        result = merge_memory(db, context, memory_id=memory_id, payload=body)
    except ValueError as e:
        raise ApiError(409, "bad_request", str(e))

    return envelope(
        result.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ──────────────────────────────────────────────────────────────────────
# POST /memory/{memory_id}/expire
# ──────────────────────────────────────────────────────────────────────

@router.post("/{memory_id}/expire", response_model=dict)
def expire_memory_endpoint(
    memory_id: UUID,
    body: MemoryStatusUpdate = MemoryStatusUpdate(),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Expire a memory (active → expired).

    Sets ``expired_at = now()``. Only active memories can be expired.
    """
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "Idempotency-Key header required")

    existing = get_memory(db, memory_id)
    if existing is None:
        raise ApiError(404, "bad_request", f"memory {memory_id} not found")

    if existing.status != "active":
        raise ApiError(
            409,
            "bad_request",
            f"memory {memory_id} is '{existing.status}', only 'active' can be expired",
        )

    try:
        result = expire_memory(db, context, memory_id=memory_id)
    except ValueError as e:
        raise ApiError(409, "bad_request", str(e))

    return envelope(
        result.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ──────────────────────────────────────────────────────────────────────
# POST /memory/{memory_id}/restore
# ──────────────────────────────────────────────────────────────────────

@router.post("/{memory_id}/restore", response_model=dict)
def restore_memory_endpoint(
    memory_id: UUID,
    body: MemoryStatusUpdate = MemoryStatusUpdate(),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Restore a memory (expired|deleted → active).

    Clears ``expired_at``. The CHECK constraint requires that an active
    memory has ``activated_by_review_item_id`` set — this is preserved
    from the original activation.
    """
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "Idempotency-Key header required")

    existing = get_memory(db, memory_id)
    if existing is None:
        raise ApiError(404, "bad_request", f"memory {memory_id} not found")

    if existing.status not in ("expired", "deleted"):
        raise ApiError(
            409,
            "bad_request",
            f"memory {memory_id} is '{existing.status}', only 'expired' or 'deleted' can be restored",
        )

    try:
        result = restore_memory(db, context, memory_id=memory_id)
    except ValueError as e:
        raise ApiError(409, "bad_request", str(e))

    return envelope(
        result.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ──────────────────────────────────────────────────────────────────────
# DELETE /memory/{memory_id}
# ──────────────────────────────────────────────────────────────────────

@router.delete("/{memory_id}", response_model=dict)
def delete_memory_endpoint(
    memory_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Soft-delete a memory (any status → deleted).

    Does not physically remove data. Creates a ``memory_versions`` row.
    """
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "Idempotency-Key header required")

    existing = get_memory(db, memory_id)
    if existing is None:
        raise ApiError(404, "bad_request", f"memory {memory_id} not found")

    if existing.status == "deleted":
        raise ApiError(
            409, "bad_request", f"memory {memory_id} is already deleted"
        )

    try:
        result = delete_memory(db, context, memory_id=memory_id)
    except ValueError as e:
        raise ApiError(409, "bad_request", str(e))

    return envelope(
        {"deleted": True, "memory_id": str(memory_id), "status": "deleted"},
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ══════════════════════════════════════════════════════════════════════
# Batch approve / reject
# ══════════════════════════════════════════════════════════════════════


@router.post("/approve", response_model=dict, status_code=200)
def batch_approve_endpoint(
    body: MemoryBatchApproveRequest,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Batch approve multiple draft memories → active.

    Accepts a list of ``memory_ids`` and approves each one that is in
    ``draft`` status.  Each approval is independent — a failure on one
    does not stop the batch.
    """
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "Idempotency-Key header required")

    result = batch_approve_memories(
        db,
        context,
        memory_ids=body.memory_ids,
        review_item_id=body.review_item_id,
        reason=body.reason,
    )

    return envelope(
        result.__dict__,
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.post("/reject", response_model=dict, status_code=200)
def batch_reject_endpoint(
    body: MemoryBatchRejectRequest,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Batch reject multiple draft memories → deleted.

    Accepts a list of ``memory_ids`` and rejects each one that is in
    ``draft`` status.  Each rejection is independent — a failure on one
    does not stop the batch.
    """
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "Idempotency-Key header required")

    result = batch_reject_memories(
        db,
        context,
        memory_ids=body.memory_ids,
        reason=body.reason,
    )

    return envelope(
        result.__dict__,
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ══════════════════════════════════════════════════════════════════════
# P4-06 — Memory Versions (read-only history)
# ══════════════════════════════════════════════════════════════════════

@router.get("/{memory_id}/versions", response_model=dict)
def list_memory_versions_endpoint(
    memory_id: UUID,
    pagination: PaginationParams = Depends(),
    filters: MemoryVersionFilterParams = Depends(),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """List version history for a memory, newest-first."""
    from mneme.db.memory_versions import list_memory_versions as _list_versions

    existing = get_memory(db, memory_id)
    if existing is None:
        raise ApiError(404, "bad_request", f"memory {memory_id} not found")

    items, total = _list_versions(
        db,
        memory_id=memory_id,
        action=filters.action.value if filters.action else None,
        page=pagination.page,
        page_size=pagination.page_size,
    )

    pi = _page_info(total, pagination.page, pagination.page_size)
    data = MemoryVersionListResponse(
        items=[MemoryVersionRead.model_validate(v.model_dump(mode="json")) for v in items],
        page_info=pi,
    )

    return envelope(
        data.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.get("/{memory_id}/versions/{version}", response_model=dict)
def get_memory_version_endpoint(
    memory_id: UUID,
    version: int,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Get a specific version of a memory by version number."""
    from mneme.db.memory_versions import get_memory_version as _get_version

    existing = get_memory(db, memory_id)
    if existing is None:
        raise ApiError(404, "bad_request", f"memory {memory_id} not found")

    row = _get_version(db, memory_id=memory_id, version=version)
    if row is None:
        raise ApiError(
            404, "bad_request", f"version {version} of memory {memory_id} not found"
        )

    return envelope(
        row.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ══════════════════════════════════════════════════════════════════════
# P4-06 — Memory Sources (manual evidence links)
# ══════════════════════════════════════════════════════════════════════

_MEMORY_SOURCE_ROUTER_PREFIX = "/{memory_id}/sources"


@router.get("/{memory_id}/sources", response_model=dict)
def list_memory_sources_endpoint(
    memory_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """List all source links for a memory."""
    from mneme.db.memory_sources import list_memory_sources as _list_sources

    existing = get_memory(db, memory_id)
    if existing is None:
        raise ApiError(404, "bad_request", f"memory {memory_id} not found")

    items = _list_sources(db, memory_id=memory_id)
    data = [MemorySourceRead.model_validate(s.model_dump(mode="json")) for s in items]

    return envelope(
        {"items": data},
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.post("/{memory_id}/sources", response_model=dict, status_code=201)
def add_memory_source_endpoint(
    memory_id: UUID,
    body: MemorySourceCreate,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Add a source link to a memory version.

    At least one of ``candidate_id``, ``raw_event_id``, ``asset_id``,
    ``document_id``, ``block_id``, or ``message_id`` must be provided.
    """
    from mneme.db.memory_sources import add_memory_source as _add_source

    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "Idempotency-Key header required")

    existing = get_memory(db, memory_id)
    if existing is None:
        raise ApiError(404, "bad_request", f"memory {memory_id} not found")

    try:
        result = _add_source(db, context, memory_id=memory_id, payload=body)
    except ValueError as e:
        raise ApiError(400, "bad_request", str(e))

    return envelope(
        result.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.delete("/sources/{memory_source_id}", response_model=dict)
def remove_memory_source_endpoint(
    memory_source_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Remove a source link from a memory."""
    from mneme.db.memory_sources import remove_memory_source as _remove_source

    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "Idempotency-Key header required")

    deleted = _remove_source(db, context, memory_source_id=memory_source_id)

    if not deleted:
        raise ApiError(404, "bad_request", f"memory_source {memory_source_id} not found")

    return envelope(
        {"deleted": True, "memory_source_id": str(memory_source_id)},
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════
# P4-09: Memory Extract Pipeline
# ═══════════════════════════════════════════════════════════════════════════


@router.post("/extract", response_model=dict, status_code=200)
def trigger_extract(
    body: MemoryExtractRequest,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Manually trigger the Memory Extract Pipeline for a source message or raw_event.

    The pipeline:
    1. Resolves the source text from the message/event.
    2. Calls the LLM via Gateway to extract candidate memories.
    3. Submits candidates (idempotent via ``candidate_hash``).
    4. Creates ``memory_sources`` rows with evidence spans.

    Duplicate extraction of the same source is safe — ``candidate_hash``
    UNIQUE constraint ensures no duplicate candidates.
    """
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "Idempotency-Key header required")

    from mneme.memory.extract_pipeline import ExtractPipelineError, run_extract_pipeline

    try:
        output = run_extract_pipeline(
            db,
            context,
            source_type=body.source_type,
            source_id=body.source_id,
            project_id=body.project_id,
            conversation_context=body.conversation_context or "",
        )
    except ExtractPipelineError as exc:
        # Business-layer classified error → map to HTTP status
        status_map = {
            "source_not_found": 404,
            "empty_source": 400,
            "gateway_timeout": 502,
            "gateway_rate_limited": 429,
            "llm_parse_error": 422,
            "project_not_found": 404,
            "invalid_request": 400,
            "gateway_error": 502,
        }
        http_status = status_map.get(exc.code, 502)
        raise ApiError(http_status, exc.code, exc.message)
    except ValueError as exc:
        raise ApiError(400, "invalid_request", str(exc))
    except LookupError as exc:
        raise ApiError(404, "not_found", str(exc))
    except Exception as exc:
        raise ApiError(500, "extract_internal_error", str(exc))

    return envelope(
        {
            "pipeline_run_id": str(output.pipeline_run_id) if output.pipeline_run_id else None,
            "candidates_submitted": output.candidates_submitted,
            "candidates_deduped": output.candidates_deduped,
            "sources_created": output.sources_created,
            "llm_candidates_found": output.llm_candidates_found,
            "error": output.error,
            "candidates": output.candidates,
        },
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════
# P4-11: Memory Decay & Reinforcement API
# ═══════════════════════════════════════════════════════════════════════════


@router.post("/decay", response_model=dict, status_code=200)
def trigger_decay_endpoint(
    limit: int | None = None,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Manually trigger a decay batch on active memories.

    Applies time-decay to active memories ordered by least-recently-decayed.
    Emotion_charge modulates the effective decay rate per memory.
    """
    from mneme.memory.decay import apply_decay_batch

    result = apply_decay_batch(db, limit=limit)

    transitions_data = [
        DecayStateTransition(
            memory_id=t.memory_id,
            canonical_key=t.canonical_key,
            from_state=t.from_state,
            to_state=t.to_state,
            decay_score=t.decay_score,
        ).model_dump(mode="json")
        for t in result.transitions
    ]

    return envelope(
        {
            "memories_processed": result.total_processed,
            "scores_updated": result.scores_updated,
            "transitions": len(result.transitions),
            "transition_details": transitions_data,
            "errors": result.errors,
        },
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.get("/decay-status", response_model=dict)
def decay_status_endpoint(
    project_id: UUID | None = None,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Get decay state summary for a project (or globally).

    Returns counts by decay_state (active/decaying/silent/archived)
    and average decay_score.
    """
    from mneme.memory.decay import get_decay_status

    status = get_decay_status(db, project_id=project_id)
    data = DecayStatusResponse.model_validate(status)

    return envelope(
        data.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.post("/{memory_id}/reinforce", response_model=dict)
def reinforce_memory_endpoint(
    memory_id: UUID,
    body: ReinforceRequest = ReinforceRequest(),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Apply a reinforcement bonus to a specific memory.

    Boosts ``decay_score`` (capped at 1.0) and may transition
    ``decay_state`` toward active.  Records a version row
    (action='reinforce').  Use when a memory is accessed via search
    hit, LLM recall, or explicit pin.
    """
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "Idempotency-Key header required")

    from mneme.memory.decay import reinforce_memory

    try:
        result = reinforce_memory(
            db,
            context,
            memory_id=memory_id,
            bonus=body.bonus,
            reason=body.reason,
        )
    except ValueError as e:
        raise ApiError(400, "bad_request", str(e))

    return envelope(
        result,
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════
# P4-12: Emotion Inference API
# ═══════════════════════════════════════════════════════════════════════════


@router.post("/emotion-infer", response_model=dict, status_code=200)
def trigger_emotion_infer_endpoint(
    body: EmotionInferRequest = EmotionInferRequest(),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Manually trigger emotion inference on active memories.

    Runs the behavior-based inference engine on memory_text +
    interaction patterns (reinforcement count, version churn, age).
    Infers ``emotion_charge`` (neutral/embarrassed/proud/fearful)
    and ``uncertainty_score`` (0.0=certain, 1.0=pure guess).
    Only memories not inferred recently (or with high uncertainty)
    are processed.
    """
    from mneme.memory.emotion import apply_emotion_inference_batch

    result = apply_emotion_inference_batch(db, limit=body.limit)

    return envelope(
        {
            "total_processed": result.total_processed,
            "emotions_updated": result.emotions_updated,
            "emotion_counts": result.emotion_counts,
            "avg_uncertainty": result.avg_uncertainty,
            "errors": result.errors,
        },
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.get("/emotion-status", response_model=dict)
def emotion_status_endpoint(
    project_id: UUID | None = None,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Get emotion distribution summary for a project (or globally).

    Returns counts by emotion_charge (neutral/proud/embarrassed/fearful)
    and average uncertainty_score.
    """
    from mneme.memory.emotion import get_emotion_status

    status = get_emotion_status(db, project_id=project_id)
    data = EmotionStatusResponse.model_validate(status)

    return envelope(
        data.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )
