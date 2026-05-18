"""Messages API routes (P4-02).

Endpoints
---------
* ``POST   /api/v4/conversations/{conversation_id}/messages`` — Write a message
* ``POST   /api/v4/conversations/{conversation_id}/messages/batch`` — Batch import messages
* ``GET    /api/v4/conversations/{conversation_id}/messages`` — List messages (paginated)
* ``GET    /api/v4/conversations/{conversation_id}/messages/{message_id}`` — Message detail

Key design decisions:
* Messages are **immutable** — no PATCH or DELETE endpoints.
* ``content_hash`` is auto-computed (SHA-256 of content_text) on every write.
* ``UNIQUE(event_source_id, content_hash, message_time)`` prevents duplicates.
* Batch import is all-or-nothing within a single transaction (max 500).
* Conversation ``started_at`` is auto-set on first message.
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
from mneme.db.conversations import get_conversation
from mneme.db.idempotency import check_idempotency_key_any
from mneme.db.messages import (
    create_message,
    create_message_batch,
    get_message,
    list_messages,
)
from mneme.schemas import (
    PaginatedData,
    PageInfo,
    ResponseEnvelope,
)
from mneme.schemas.conversations import (
    BatchImportResult,
    MessageBatchCreate,
    MessageCreate,
    MessageRead,
    ConversationRead,
)

router = APIRouter(tags=["messages"])


# ══════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════

def _require_conversation(db: Session, conversation_id: UUID) -> ConversationRead:
    """Validate that a conversation exists and is not deleted."""
    conv = get_conversation(db, conversation_id)
    if conv is None:
        raise ApiError(404, "not_found", f"对话 {conversation_id} 不存在")
    if conv.conversation_status == "deleted":
        raise ApiError(400, "bad_request", f"对话 {conversation_id} 已被删除")
    return conv


def _require_conversation_active(db: Session, conversation_id: UUID) -> ConversationRead:
    """Validate that a conversation exists and is active (for writes)."""
    conv = get_conversation(db, conversation_id)
    if conv is None:
        raise ApiError(404, "not_found", f"对话 {conversation_id} 不存在")
    if conv.conversation_status == "archived":
        raise ApiError(400, "bad_request", f"对话 {conversation_id} 已归档，无法写入消息")
    if conv.conversation_status == "deleted":
        raise ApiError(400, "bad_request", f"对话 {conversation_id} 已被删除")
    return conv


def _require_message(db: Session, message_id: UUID) -> MessageRead:
    """Fetch and validate that a message exists."""
    msg = get_message(db, message_id)
    if msg is None:
        raise ApiError(404, "not_found", f"消息 {message_id} 不存在")
    return msg


def _guard_idempotency(db: Session, key: str, expected_type: str) -> None:
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
# POST /api/v4/conversations/{conversation_id}/messages — Write message
# ══════════════════════════════════════════════════════════════════════

@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=ResponseEnvelope[MessageRead],
)
def create_message_route(
    conversation_id: UUID,
    payload: MessageCreate,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Write a single message into a conversation.

    * ``content_hash`` is automatically computed as the SHA-256 of ``content_text``.
    * ``pii_flags`` defaults to ``[]``.
    * If the conversation has no ``started_at``, it is set to this message's ``message_time``.
    * Duplicate messages (same event_source_id + content_hash + message_time) return the existing message.
    * Messages are **immutable** after creation.

    Requires ``Idempotency-Key`` header.
    """
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "写请求必须提供 Idempotency-Key 头")

    _guard_idempotency(db, context.idempotency_key, "message")
    conv = _require_conversation_active(db, conversation_id)
    context = _resolve_actor(auth, context)

    msg = create_message(
        db,
        context,
        conversation_id=conversation_id,
        payload=payload,
        project_id=conv.project_id,
    )

    data = jsonable_encoder(msg)
    return envelope(data, request_id=context.request_id, correlation_id=context.correlation_id)


# ══════════════════════════════════════════════════════════════════════
# POST /api/v4/conversations/{conversation_id}/messages/batch — Batch
# ══════════════════════════════════════════════════════════════════════

@router.post(
    "/conversations/{conversation_id}/messages/batch",
    response_model=ResponseEnvelope[BatchImportResult],
)
def create_message_batch_route(
    conversation_id: UUID,
    payload: MessageBatchCreate,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Batch-import up to 500 messages into a conversation.

    * All messages land in a single transaction — partial failure = full rollback.
    * Each message's ``content_hash`` is auto-computed from its ``content_text``.
    * Duplicate messages (same event_source_id + content_hash + message_time) are skipped.
    * Conversation ``started_at`` is set from the earliest message_time if currently NULL.

    Requires ``Idempotency-Key`` header.
    """
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "写请求必须提供 Idempotency-Key 头")

    _guard_idempotency(db, context.idempotency_key, "message")
    conv = _require_conversation_active(db, conversation_id)
    context = _resolve_actor(auth, context)

    if len(payload.messages) > 500:
        raise ApiError(400, "bad_request", f"批量消息导入最多 500 条，当前 {len(payload.messages)} 条")

    result = create_message_batch(
        db,
        context,
        conversation_id=conversation_id,
        messages=payload.messages,
        batch_event_source_id=payload.event_source_id,
        project_id=conv.project_id,
    )

    data = jsonable_encoder(result)
    return envelope(data, request_id=context.request_id, correlation_id=context.correlation_id)


# ══════════════════════════════════════════════════════════════════════
# GET /api/v4/conversations/{conversation_id}/messages — List messages
# ══════════════════════════════════════════════════════════════════════

@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=ResponseEnvelope[PaginatedData[MessageRead]],
)
def list_messages_route(
    conversation_id: UUID,
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(50, ge=1, le=200, description="每页数量"),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """List messages for a conversation, ordered by message_time ASC.

    Supports infinite-scroll style pagination. Use page=1 for the earliest
    messages and increase page to go forward in time.
    """
    _require_conversation(db, conversation_id)

    items, total = list_messages(
        db,
        conversation_id=conversation_id,
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
# GET /api/v4/conversations/{conversation_id}/messages/{message_id} — Detail
# ══════════════════════════════════════════════════════════════════════

@router.get(
    "/conversations/{conversation_id}/messages/{message_id}",
    response_model=ResponseEnvelope[MessageRead],
)
def get_message_route(
    conversation_id: UUID,
    message_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Get a single message's detail, including parent message reference.

    Validates that the message belongs to the specified conversation.
    """
    _require_conversation(db, conversation_id)

    msg = _require_message(db, message_id)

    # Cross-validate message belongs to this conversation
    if msg.conversation_id != conversation_id:
        raise ApiError(
            404,
            "not_found",
            f"消息 {message_id} 不属于对话 {conversation_id}",
        )

    data = jsonable_encoder(msg)
    return envelope(data, request_id=context.request_id, correlation_id=context.correlation_id)

