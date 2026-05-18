"""Event Source API routes (P4-02).

Endpoints
---------
* ``POST   /api/v4/conversations/{conversation_id}/event-sources`` — Create event source
* ``GET    /api/v4/conversations/{conversation_id}/event-sources`` — List event sources

Nested under conversations to express the conversation→event_source→messages hierarchy.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
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
from mneme.db.event_source import (
    create_event_source,
    list_event_sources,
)
from mneme.db.idempotency import check_idempotency_key_any
from mneme.schemas import ResponseEnvelope
from mneme.schemas.conversations import (
    EventSourceCreate,
    EventSourceRead,
)

router = APIRouter(tags=["event-sources"])


# ══════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════

def _require_conversation(db: Session, conversation_id: UUID) -> None:
    """Validate that a conversation exists (raises 404)."""
    conv = get_conversation(db, conversation_id)
    if conv is None:
        raise ApiError(404, "not_found", f"对话 {conversation_id} 不存在")


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
# POST /api/v4/conversations/{conversation_id}/event-sources — Create
# ══════════════════════════════════════════════════════════════════════

@router.post(
    "/conversations/{conversation_id}/event-sources",
    response_model=ResponseEnvelope[EventSourceRead],
)
def create_event_source_route(
    conversation_id: UUID,
    payload: EventSourceCreate,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Create an event source segment under a conversation.

    A conversation can have multiple event sources (e.g. same dialogue appeared
    in Slack and then in email).

    Requires ``Idempotency-Key`` header.
    """
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "写请求必须提供 Idempotency-Key 头")

    _guard_idempotency(db, context.idempotency_key, "event_source")
    conv = get_conversation(db, conversation_id)
    if conv is None:
        raise ApiError(404, "not_found", f"对话 {conversation_id} 不存在")

    context = _resolve_actor(auth, context)

    es = create_event_source(
        db,
        context,
        conversation_id=conversation_id,
        payload=payload,
        project_id=conv.project_id,
    )

    data = jsonable_encoder(es)
    return envelope(data, request_id=context.request_id, correlation_id=context.correlation_id)


# ══════════════════════════════════════════════════════════════════════
# GET /api/v4/conversations/{conversation_id}/event-sources — List
# ══════════════════════════════════════════════════════════════════════

@router.get(
    "/conversations/{conversation_id}/event-sources",
    response_model=ResponseEnvelope[list[EventSourceRead]],
)
def list_event_sources_route(
    conversation_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """List all event source segments for a conversation."""
    _require_conversation(db, conversation_id)

    sources = list_event_sources(db, conversation_id=conversation_id)

    data = jsonable_encoder(sources)
    return envelope(data, request_id=context.request_id, correlation_id=context.correlation_id)

