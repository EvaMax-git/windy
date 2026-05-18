"""P4-04 Memory Candidates API — CRUD + status transitions.

Endpoints
---------
* ``POST   /api/v4/memory/candidates``                  — submit candidate
* ``GET    /api/v4/memory/candidates``                  — list (paginated, filterable)
* ``GET    /api/v4/memory/candidates/{candidate_id}``   — get detail
* ``PATCH  /api/v4/memory/candidates/{candidate_id}``   — update fields
* ``POST   /api/v4/memory/candidates/{candidate_id}/approve`` — approve candidate
* ``POST   /api/v4/memory/candidates/{candidate_id}/reject``  — reject candidate
* ``DELETE /api/v4/memory/candidates/{candidate_id}``   — delete candidate
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
from mneme.db.memory_candidates import (
    delete_candidate,
    get_candidate_by_id,
    list_candidates,
    submit_candidate,
    update_candidate,
    update_candidate_status,
)
from mneme.schemas.common import PageInfo, PaginationParams
from mneme.schemas.memory_candidates import (
    CandidateFilterParams,
    CandidateSourceType,
    CandidateStatus,
    MemoryCandidateCreate,
    MemoryCandidateListResponse,
    MemoryCandidateRead,
    MemoryCandidateStatusUpdate,
    MemoryCandidateUpdate,
)

router = APIRouter(prefix="/memory-candidates", tags=["memory"])


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
# POST /memory/candidates
# ──────────────────────────────────────────────────────────────────────

@router.post("", response_model=dict, status_code=201)
def submit_candidate_endpoint(
    body: MemoryCandidateCreate,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Submit a new memory candidate (idempotent).

    ``candidate_hash`` = SHA-256(title + candidate_text + source_type + source_id)
    is computed automatically for dedup.  Duplicate submissions of the same content
    return the existing candidate with 200 OK (no 409 conflict).

    Repeated calls with the same ``Idempotency-Key`` header also return the
    original result.
    """
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "Idempotency-Key header required")

    try:
        result = submit_candidate(db, context, payload=body)
    except IntegrityError:
        raise ApiError(
            409,
            "idempotency_conflict",
            "A candidate with the same hash already exists in this project",
        )

    return envelope(
        result.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ──────────────────────────────────────────────────────────────────────
# GET /memory/candidates
# ──────────────────────────────────────────────────────────────────────

@router.get("", response_model=dict)
def list_candidates_endpoint(
    pagination: PaginationParams = Depends(),
    filters: CandidateFilterParams = Depends(),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """List memory candidates with optional filters and pagination."""
    rows, total = list_candidates(
        db,
        project_id=filters.project_id,
        source_type=filters.source_type.value if filters.source_type else None,
        candidate_status=filters.candidate_status.value if filters.candidate_status else None,
        created_after=filters.created_after,
        created_before=filters.created_before,
        page=pagination.page,
        page_size=pagination.page_size,
    )

    items = [MemoryCandidateRead.model_validate(r.model_dump(mode="json")) for r in rows]
    pi = _page_info(total, pagination.page, pagination.page_size)
    data = MemoryCandidateListResponse(items=items, page_info=pi)

    return envelope(
        data.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ──────────────────────────────────────────────────────────────────────
# GET /memory/candidates/{candidate_id}
# ──────────────────────────────────────────────────────────────────────

@router.get("/{candidate_id}", response_model=dict)
def get_candidate_endpoint(
    candidate_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Get a single memory candidate by ID."""
    row = get_candidate_by_id(db, candidate_id)
    if row is None:
        raise ApiError(404, "bad_request", f"candidate {candidate_id} not found")

    return envelope(
        row.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ──────────────────────────────────────────────────────────────────────
# PATCH /memory/candidates/{candidate_id}
# ──────────────────────────────────────────────────────────────────────

@router.patch("/{candidate_id}", response_model=dict)
def update_candidate_endpoint(
    candidate_id: UUID,
    body: MemoryCandidateUpdate,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Update mutable fields: title, candidate_text, sensitivity_level,
    confidence_score, metadata_json."""
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "Idempotency-Key header required")

    existing = get_candidate_by_id(db, candidate_id)
    if existing is None:
        raise ApiError(404, "bad_request", f"candidate {candidate_id} not found")

    try:
        result = update_candidate(db, context, candidate_id=candidate_id, payload=body)
    except ValueError as e:
        raise ApiError(404, "bad_request", str(e))

    return envelope(
        result.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ──────────────────────────────────────────────────────────────────────
# POST /memory/candidates/{candidate_id}/approve
# ──────────────────────────────────────────────────────────────────────

@router.post("/{candidate_id}/approve", response_model=dict)
def approve_candidate_endpoint(
    candidate_id: UUID,
    body: MemoryCandidateStatusUpdate = MemoryCandidateStatusUpdate(),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Approve a candidate (``pending_review`` → ``approved``).

    Only candidates in ``pending_review`` status can be approved.
    """
    existing = get_candidate_by_id(db, candidate_id)
    if existing is None:
        raise ApiError(404, "bad_request", f"candidate {candidate_id} not found")

    if existing.candidate_status != CandidateStatus.pending_review.value:
        raise ApiError(
            409,
            "bad_request",
            f"candidate {candidate_id} is '{existing.candidate_status}', only 'pending_review' can be approved",
        )

    result = update_candidate_status(
        db,
        candidate_id=candidate_id,
        from_status=CandidateStatus.pending_review.value,
        to_status=CandidateStatus.approved.value,
    )
    if result is None:
        raise ApiError(
            409,
            "bad_request",
            f"candidate {candidate_id} could not be approved (may have been modified concurrently)",
        )

    return envelope(
        result.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ──────────────────────────────────────────────────────────────────────
# POST /memory/candidates/{candidate_id}/reject
# ──────────────────────────────────────────────────────────────────────

@router.post("/{candidate_id}/reject", response_model=dict)
def reject_candidate_endpoint(
    candidate_id: UUID,
    body: MemoryCandidateStatusUpdate = MemoryCandidateStatusUpdate(),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Reject a candidate (``pending_review`` → ``rejected``).

    Only candidates in ``pending_review`` status can be rejected.
    """
    existing = get_candidate_by_id(db, candidate_id)
    if existing is None:
        raise ApiError(404, "bad_request", f"candidate {candidate_id} not found")

    if existing.candidate_status != CandidateStatus.pending_review.value:
        raise ApiError(
            409,
            "bad_request",
            f"candidate {candidate_id} is '{existing.candidate_status}', only 'pending_review' can be rejected",
        )

    result = update_candidate_status(
        db,
        candidate_id=candidate_id,
        from_status=CandidateStatus.pending_review.value,
        to_status=CandidateStatus.rejected.value,
    )
    if result is None:
        raise ApiError(
            409,
            "bad_request",
            f"candidate {candidate_id} could not be rejected (may have been modified concurrently)",
        )

    return envelope(
        result.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ──────────────────────────────────────────────────────────────────────
# DELETE /memory/candidates/{candidate_id}
# ──────────────────────────────────────────────────────────────────────

@router.delete("/{candidate_id}", response_model=dict)
def delete_candidate_endpoint(
    candidate_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Delete a memory candidate (hard delete)."""
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "Idempotency-Key header required")

    existing = get_candidate_by_id(db, candidate_id)
    if existing is None:
        raise ApiError(404, "bad_request", f"candidate {candidate_id} not found")

    try:
        deleted = delete_candidate(db, context, candidate_id=candidate_id)
    except ValueError as e:
        raise ApiError(404, "bad_request", str(e))

    return envelope(
        {"deleted": deleted is not None, "candidate_id": str(candidate_id)},
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )
