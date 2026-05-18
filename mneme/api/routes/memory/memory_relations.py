"""P4-08 Memory Relations API.

Endpoints
---------
* ``POST   /api/v4/memory/relations``                        — create relation
* ``GET    /api/v4/memory/{memory_id}/relations``             — list relations for a memory
* ``GET    /api/v4/memory/relations/{memory_relation_id}``     — relation detail
* ``PATCH  /api/v4/memory/relations/{memory_relation_id}``     — update relation
* ``POST   /api/v4/memory/relations/{memory_relation_id}/resolve`` — mark resolved
* ``POST   /api/v4/memory/relations/{memory_relation_id}/cancel``  — cancel relation
"""

from __future__ import annotations

import math
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mneme.api.context import RequestContext, get_request_context
from mneme.api.errors import ApiError
from mneme.api.schemas import envelope
from mneme.db.base import get_db
from mneme.db.memory_relations import (
    cancel_relation,
    create_memory_relation,
    get_memory_relation,
    list_memory_relations,
    resolve_relation,
    update_memory_relation,
)
from mneme.schemas.common import PageInfo, PaginationParams
from mneme.schemas.memory_relations import (
    MemoryRelationCreate,
    MemoryRelationListResponse,
    MemoryRelationRead,
    MemoryRelationUpdate,
)

router = APIRouter(prefix="/memory", tags=["memory-relations"])


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
# POST /memory/relations
# ──────────────────────────────────────────────────────────────────────

@router.post("/relations", response_model=dict, status_code=201)
def create_relation_endpoint(
    body: MemoryRelationCreate,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Create a memory relation between two memories.

    ``UNIQUE(from_memory_id, to_memory_id, relation_type)`` prevents duplicate
    relations for the same direction and type.
    ``CHECK(from_memory_id <> to_memory_id)`` prevents self-referencing.
    """
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "Idempotency-Key header required")

    try:
        result = create_memory_relation(db, context, payload=body)
    except ValueError as e:
        raise ApiError(400, "bad_request", str(e))
    except IntegrityError:
        raise ApiError(
            409,
            "idempotency_conflict",
            "A relation of this type already exists between these two memories"
            " or self-reference is not allowed.",
        )

    return envelope(
        result.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ──────────────────────────────────────────────────────────────────────
# GET /memory/{memory_id}/relations
# ──────────────────────────────────────────────────────────────────────

@router.get("/{memory_id}/relations", response_model=dict)
def list_relations_for_memory_endpoint(
    memory_id: UUID,
    pagination: PaginationParams = Depends(),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """List all relations involving *memory_id* (both from and to directions)."""
    items, total = list_memory_relations(
        db,
        memory_id=memory_id,
        page=pagination.page,
        page_size=pagination.page_size,
    )

    pi = _page_info(total, pagination.page, pagination.page_size)
    data = MemoryRelationListResponse(
        items=[MemoryRelationRead.model_validate(r.model_dump(mode="json")) for r in items],
        page_info=pi,
    )

    return envelope(
        data.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ──────────────────────────────────────────────────────────────────────
# GET /memory/relations/{memory_relation_id}
# ──────────────────────────────────────────────────────────────────────

@router.get("/relations/{memory_relation_id}", response_model=dict)
def get_relation_endpoint(
    memory_relation_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Get a single memory relation by ID."""
    row = get_memory_relation(db, memory_relation_id)
    if row is None:
        raise ApiError(404, "bad_request", f"memory_relation {memory_relation_id} not found")

    return envelope(
        row.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ──────────────────────────────────────────────────────────────────────
# PATCH /memory/relations/{memory_relation_id}
# ──────────────────────────────────────────────────────────────────────

@router.patch("/relations/{memory_relation_id}", response_model=dict)
def update_relation_endpoint(
    memory_relation_id: UUID,
    body: MemoryRelationUpdate,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Update mutable fields of a memory relation (reason, metadata_json)."""
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "Idempotency-Key header required")

    existing = get_memory_relation(db, memory_relation_id)
    if existing is None:
        raise ApiError(404, "bad_request", f"memory_relation {memory_relation_id} not found")

    try:
        result = update_memory_relation(
            db, context, memory_relation_id=memory_relation_id, payload=body
        )
    except ValueError as e:
        raise ApiError(400, "bad_request", str(e))

    return envelope(
        result.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ──────────────────────────────────────────────────────────────────────
# POST /memory/relations/{memory_relation_id}/resolve
# ──────────────────────────────────────────────────────────────────────

@router.post("/relations/{memory_relation_id}/resolve", response_model=dict)
def resolve_relation_endpoint(
    memory_relation_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Mark a memory relation as resolved (active → resolved)."""
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "Idempotency-Key header required")

    existing = get_memory_relation(db, memory_relation_id)
    if existing is None:
        raise ApiError(404, "bad_request", f"memory_relation {memory_relation_id} not found")
    if existing.relation_status != "active":
        raise ApiError(
            409,
            "bad_request",
            f"memory_relation {memory_relation_id} is '{existing.relation_status}', "
            f"only 'active' can be resolved",
        )

    try:
        result = resolve_relation(db, context, memory_relation_id=memory_relation_id)
    except ValueError as e:
        raise ApiError(400, "bad_request", str(e))

    return envelope(
        result.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ──────────────────────────────────────────────────────────────────────
# POST /memory/relations/{memory_relation_id}/cancel
# ──────────────────────────────────────────────────────────────────────

@router.post("/relations/{memory_relation_id}/cancel", response_model=dict)
def cancel_relation_endpoint(
    memory_relation_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Cancel a memory relation (active → cancelled)."""
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "Idempotency-Key header required")

    existing = get_memory_relation(db, memory_relation_id)
    if existing is None:
        raise ApiError(404, "bad_request", f"memory_relation {memory_relation_id} not found")
    if existing.relation_status != "active":
        raise ApiError(
            409,
            "bad_request",
            f"memory_relation {memory_relation_id} is '{existing.relation_status}', "
            f"only 'active' can be cancelled",
        )

    try:
        result = cancel_relation(db, context, memory_relation_id=memory_relation_id)
    except ValueError as e:
        raise ApiError(400, "bad_request", str(e))

    return envelope(
        result.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )
