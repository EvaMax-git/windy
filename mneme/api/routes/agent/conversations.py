"""Conversation API routes (P4-01).

Endpoints
---------
* ``POST   /api/v4/conversations`` — Create a conversation
* ``GET    /api/v4/conversations`` — List conversations (paginated, filterable)
* ``GET    /api/v4/conversations/{id}`` — Get conversation detail
* ``PATCH  /api/v4/conversations/{id}`` — Update conversation
* ``POST   /api/v4/conversations/{id}/archive`` — Archive conversation
* ``POST   /api/v4/conversations/{id}/delete`` — Soft-delete conversation

Every write endpoint requires an ``Idempotency-Key`` header.
"""

from __future__ import annotations

from typing import Optional
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
from mneme.db.conversations import (
    archive_conversation,
    create_conversation,
    delete_conversation,
    get_conversation,
    list_conversations,
    update_conversation,
)
from mneme.db.idempotency import check_idempotency_key_any
from mneme.db.projects import get_project
from mneme.schemas import (
    PaginatedData,
    PageInfo,
    ResponseEnvelope,
)
from mneme.schemas.conversations import (
    ConversationCreateRequest,
    ConversationListResponse,
    ConversationRead,
    ConversationUpdateRequest,
)

router = APIRouter(prefix="/conversations", tags=["conversations"])


# ══════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════

def _require_conversation(db: Session, conversation_id: UUID) -> ConversationRead:
    """Fetch and validate that a conversation exists (raises 404)."""
    conv = get_conversation(db, conversation_id)
    if conv is None:
        raise ApiError(404, "not_found", f"对话 {conversation_id} 不存在")
    return conv


def _guard_idempotency(db: Session, key: str, expected_type: str) -> None:
    """Reject if idempotency key was used for a different aggregate type."""
    existing = check_idempotency_key_any(db, idempotency_key=key)
    if existing is None:
        return
    _event_id, actual_type, _aggregate_id = existing
    if actual_type != expected_type:
        raise ApiError(
            409,
            "idempotency_conflict",
            f"幂等键已被用于 '{actual_type}'，而非 '{expected_type}'",
            details={
                "expected_aggregate_type": expected_type,
                "existing_aggregate_type": actual_type,
            },
        )


def _resolve_actor(auth: AuthenticatedSession, context: RequestContext) -> RequestContext:
    """Wire the authenticated user into the request context's actor."""
    return with_actor(
        context,
        actor=ActorContext(
            actor_type="user",
            actor_id=auth.user.user_id,
            auth_context_type="user_session",
            auth_context_id=auth.session.session_id,
        ),
    )


# ══════════════════════════════════════════════════════════════════════
# POST /api/v4/conversations — Create
# ══════════════════════════════════════════════════════════════════════

@router.post("", response_model=ResponseEnvelope[ConversationRead])
def create_conversation_route(
    payload: ConversationCreateRequest,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Create a conversation container.

    Requires ``Idempotency-Key`` header.
    """
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "写请求必须提供 Idempotency-Key 头")

    _guard_idempotency(db, context.idempotency_key, "conversation")
    context = _resolve_actor(auth, context)

    # Validate project exists
    project = get_project(db, payload.project_id)
    if project is None:
        raise ApiError(404, "not_found", f"项目 {payload.project_id} 不存在")

    conv = create_conversation(db, context, payload=payload)

    data = jsonable_encoder(conv)
    return envelope(data, request_id=context.request_id, correlation_id=context.correlation_id)


# ══════════════════════════════════════════════════════════════════════
# GET /api/v4/conversations — List
# ══════════════════════════════════════════════════════════════════════

@router.get("", response_model=ResponseEnvelope[ConversationListResponse])
def list_conversations_route(
    project_id: Optional[UUID] = Query(None, description="按项目过滤"),
    conversation_type: Optional[str] = Query(None, description="按类型过滤"),
    conversation_status: Optional[str] = Query(None, description="按状态过滤 (默认排除 deleted)"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(50, ge=1, le=200, description="每页数量"),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """List conversations with optional filters and pagination."""
    # Default: hide deleted conversations
    if conversation_status is None:
        conversation_status = None  # Show all except in query

    items, total = list_conversations(
        db,
        project_id=project_id,
        conversation_type=conversation_type,
        conversation_status=conversation_status,
        page=page,
        page_size=page_size,
    )

    total_pages = (total + page_size - 1) // page_size if total > 0 else 0

    paginated = PaginatedData(
        items=items,
        page_info=PageInfo(
            page=page,
            page_size=page_size,
            total_items=total,
            total_pages=total_pages,
            has_next=page < total_pages,
            has_previous=page > 1,
        ),
    )

    data = jsonable_encoder(paginated)
    return envelope(data, request_id=context.request_id, correlation_id=context.correlation_id)


# ══════════════════════════════════════════════════════════════════════
# GET /api/v4/conversations/{conversation_id} — Detail
# ══════════════════════════════════════════════════════════════════════

@router.get("/{conversation_id}", response_model=ResponseEnvelope[ConversationRead])
def get_conversation_route(
    conversation_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Get conversation detail including associated event_source list."""
    conv = _require_conversation(db, conversation_id)
    data = jsonable_encoder(conv)
    return envelope(data, request_id=context.request_id, correlation_id=context.correlation_id)


# ══════════════════════════════════════════════════════════════════════
# PATCH /api/v4/conversations/{conversation_id} — Update
# ══════════════════════════════════════════════════════════════════════

@router.patch("/{conversation_id}", response_model=ResponseEnvelope[ConversationRead])
def update_conversation_route(
    conversation_id: UUID,
    payload: ConversationUpdateRequest,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Update conversation mutable fields (title, sensitivity_level, retention_days).

    Requires ``Idempotency-Key`` header.
    """
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "写请求必须提供 Idempotency-Key 头")

    _guard_idempotency(db, context.idempotency_key, "conversation")
    _require_conversation(db, conversation_id)
    context = _resolve_actor(auth, context)

    try:
        conv = update_conversation(db, context, conversation_id=conversation_id, payload=payload)
    except ValueError as e:
        raise ApiError(400, "bad_request", str(e))

    data = jsonable_encoder(conv)
    return envelope(data, request_id=context.request_id, correlation_id=context.correlation_id)


# ══════════════════════════════════════════════════════════════════════
# POST /api/v4/conversations/{conversation_id}/archive — Archive
# ══════════════════════════════════════════════════════════════════════

@router.post("/{conversation_id}/archive", response_model=ResponseEnvelope[ConversationRead])
def archive_conversation_route(
    conversation_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Archive a conversation (active → archived).

    Sets ``ended_at = now()`` automatically.

    Requires ``Idempotency-Key`` header.
    """
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "写请求必须提供 Idempotency-Key 头")

    _guard_idempotency(db, context.idempotency_key, "conversation")
    _require_conversation(db, conversation_id)
    context = _resolve_actor(auth, context)

    try:
        conv = archive_conversation(db, context, conversation_id=conversation_id)
    except ValueError as e:
        raise ApiError(400, "bad_request", str(e))

    data = jsonable_encoder(conv)
    return envelope(data, request_id=context.request_id, correlation_id=context.correlation_id)


# ══════════════════════════════════════════════════════════════════════
# POST /api/v4/conversations/{conversation_id}/delete — Soft delete
# ══════════════════════════════════════════════════════════════════════

@router.post("/{conversation_id}/delete", response_model=ResponseEnvelope[ConversationRead])
def delete_conversation_route(
    conversation_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Soft-delete a conversation (status → 'deleted').

    Does not physically delete message data.

    Requires ``Idempotency-Key`` header.
    """
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "写请求必须提供 Idempotency-Key 头")

    _guard_idempotency(db, context.idempotency_key, "conversation")
    _require_conversation(db, conversation_id)
    context = _resolve_actor(auth, context)

    try:
        conv = delete_conversation(db, context, conversation_id=conversation_id)
    except ValueError as e:
        raise ApiError(400, "bad_request", str(e))

    data = jsonable_encoder(conv)
    return envelope(data, request_id=context.request_id, correlation_id=context.correlation_id)

