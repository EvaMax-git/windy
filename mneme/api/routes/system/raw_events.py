"""Raw Events API routes (P4-03).

Endpoints
---------
* ``POST   /api/v4/raw-events`` — Write a raw event
* ``GET    /api/v4/raw-events`` — List raw events (filter by conversation/event_source)
* ``GET    /api/v4/raw-events/{raw_event_id}`` — Raw event detail
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session

from mneme.api.auth import get_current_user_session
from mneme.api.context import (
    ActorContext,
    RequestContext,
    get_request_context,
    with_actor,
)
from mneme.api.errors import ApiError
from mneme.api.schemas import envelope
from mneme.db.auth import AuthenticatedSession
from mneme.db.base import get_db
from mneme.db.idempotency import check_idempotency_key_any
from mneme.db.raw_events import create_raw_event, get_raw_event, list_raw_events
from mneme.schemas import PaginatedData, PageInfo, ResponseEnvelope
from mneme.schemas.conversations import RawEventCreate, RawEventRead

router = APIRouter(prefix="/raw-events", tags=["raw_events"])


# ══════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════

def _guard_idempotency(db: Session, key: str, expected_type: str) -> None:
    existing = check_idempotency_key_any(db, idempotency_key=key)
    if existing is None:
        return
    _event_id, actual_type, _aggregate_id = existing
    if actual_type != expected_type:
        raise ApiError(
            409, "idempotency_conflict",
            f"幂等键已被用于 '{actual_type}'，而非 '{expected_type}'",
            details={"expected_aggregate_type": expected_type, "existing_aggregate_type": actual_type},
        )


def _resolve_actor(auth: AuthenticatedSession, context: RequestContext) -> RequestContext:
    return with_actor(context, actor=ActorContext(
        actor_type="user", actor_id=auth.user.user_id,
        auth_context_type="user_session", auth_context_id=auth.session.session_id,
    ))


# ══════════════════════════════════════════════════════════════════════
# POST /api/v4/raw-events — Create
# ══════════════════════════════════════════════════════════════════════

@router.post("", response_model=ResponseEnvelope[RawEventRead])
def create_raw_event_route(
    payload: RawEventCreate,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Write a raw event into the event store.

    * ``payload_hash`` is auto-computed as SHA-256 of ``payload_json`` (sorted keys).
    * ``text_preview`` is auto-extracted (first 500 chars from payload text fields).
    * ``idempotency_key`` is auto-derived if not provided (platform:external_id or hash:time).
    * ``retention_until`` defaults to ``event_time + 365 days``.
    * ``pii_flags`` defaults to ``[]`` (Phase 4).
    * Duplicate idempotency_key returns the existing event.

    Requires ``Idempotency-Key`` header.
    """
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "写请求必须提供 Idempotency-Key 头")

    _guard_idempotency(db, context.idempotency_key, "raw_event")
    context = _resolve_actor(auth, context)

    event = create_raw_event(db, context, payload=payload)
    return envelope(jsonable_encoder(event), request_id=context.request_id, correlation_id=context.correlation_id)


# ══════════════════════════════════════════════════════════════════════
# GET /api/v4/raw-events — List
# ══════════════════════════════════════════════════════════════════════

@router.get("", response_model=ResponseEnvelope[PaginatedData[RawEventRead]])
def list_raw_events_route(
    conversation_id: UUID | None = Query(None, description="按对话过滤"),
    event_source_id: UUID | None = Query(None, description="按事件源过滤"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """List raw events, optionally filtered by conversation or event_source.

    Ordered by event_time DESC (newest first). Supports pagination.
    """
    items, total = list_raw_events(
        db,
        conversation_id=conversation_id,
        event_source_id=event_source_id,
        page=page, page_size=page_size,
    )
    total_pages = (total + page_size - 1) // page_size if total > 0 else 0
    paginated = PaginatedData(
        items=items,
        page_info=PageInfo(page=page, page_size=page_size, total_items=total,
                          total_pages=total_pages, has_next=page < total_pages, has_previous=page > 1),
    )
    return envelope(jsonable_encoder(paginated), request_id=context.request_id, correlation_id=context.correlation_id)


# ══════════════════════════════════════════════════════════════════════
# GET /api/v4/raw-events/{raw_event_id} — Detail
# ══════════════════════════════════════════════════════════════════════

@router.get("/{raw_event_id}", response_model=ResponseEnvelope[RawEventRead])
def get_raw_event_route(
    raw_event_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Get a single raw event by its ID."""
    event = get_raw_event(db, raw_event_id)
    if event is None:
        raise ApiError(404, "not_found", f"原始事件 {raw_event_id} 不存在")
    return envelope(jsonable_encoder(event), request_id=context.request_id, correlation_id=context.correlation_id)
