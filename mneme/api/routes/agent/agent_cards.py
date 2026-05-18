from __future__ import annotations

from math import ceil
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
from mneme.db.agent_cards import (
    archive_card,
    archive_tool_item,
    create_card,
    create_tool_item,
    get_card,
    get_tool_item,
    list_cards,
    list_tool_items,
    update_card,
    update_tool_item,
)
from mneme.db.auth import AuthenticatedSession
from mneme.db.base import get_db
from mneme.schemas.agent_cards import (
    AgentCardCreateRequest,
    AgentCardListResponse,
    AgentCardRead,
    AgentCardType,
    AgentCardUpdateRequest,
    AgentToolItemCreateRequest,
    AgentToolItemRead,
    AgentToolItemUpdateRequest,
)
from mneme.schemas.common import PageInfo, ResponseEnvelope

router = APIRouter(prefix="/agent-cards", tags=["agent_cards"])


def _wire_actor(context: RequestContext, auth: AuthenticatedSession) -> RequestContext:
    return with_actor(
        context,
        actor=ActorContext(
            actor_type="user",
            actor_id=auth.user.user_id,
            auth_context_type="user_session",
            auth_context_id=auth.session.session_id,
        ),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Agent Cards
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("", response_model=ResponseEnvelope[AgentCardRead], status_code=201)
def create_card_route(
    payload: AgentCardCreateRequest,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    context = _wire_actor(context, auth)
    try:
        card = create_card(db, context, payload=payload)
    except ValueError as exc:
        raise ApiError(400, "bad_request", str(exc)) from exc

    return envelope(
        jsonable_encoder(card),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.get("", response_model=ResponseEnvelope[AgentCardListResponse])
def list_cards_route(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    card_type: str | None = Query(default=None, description="Filter by card type"),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    _auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    cards, total = list_cards(db, page=page, page_size=page_size, card_type=card_type)

    data = AgentCardListResponse(
        items=cards,
        page_info=PageInfo(
            page=page,
            page_size=page_size,
            total_items=total,
            total_pages=ceil(total / page_size) if total > 0 else 0,
            has_next=(page * page_size) < total,
            has_previous=page > 1,
        ),
    )
    return envelope(
        jsonable_encoder(data),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.get("/{card_id}", response_model=ResponseEnvelope[AgentCardRead])
def get_card_route(
    card_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    _auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    card = get_card(db, card_id)
    if card is None:
        raise ApiError(404, "bad_request", f"卡牌未找到: {card_id}")

    return envelope(
        jsonable_encoder(card),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.patch("/{card_id}", response_model=ResponseEnvelope[AgentCardRead])
def update_card_route(
    card_id: UUID,
    payload: AgentCardUpdateRequest,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    context = _wire_actor(context, auth)
    card = update_card(db, context, card_id=card_id, payload=payload)
    if card is None:
        raise ApiError(404, "bad_request", f"卡牌未找到: {card_id}")

    return envelope(
        jsonable_encoder(card),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.delete("/{card_id}", response_model=ResponseEnvelope[dict])
def archive_card_route(
    card_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    context = _wire_actor(context, auth)
    ok = archive_card(db, context, card_id=card_id)
    if not ok:
        raise ApiError(404, "bad_request", f"卡牌未找到或已归档: {card_id}")

    return envelope(
        jsonable_encoder({"deleted": True, "card_id": str(card_id)}),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Agent Tool Items (under a tool card)
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/{card_id}/tools", response_model=ResponseEnvelope[AgentToolItemRead], status_code=201)
def create_tool_item_route(
    card_id: UUID,
    payload: AgentToolItemCreateRequest,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    context = _wire_actor(context, auth)
    # Ensure card exists and is a tool card
    card = get_card(db, card_id)
    if card is None:
        raise ApiError(404, "bad_request", f"卡牌未找到: {card_id}")
    if card.card_type != AgentCardType.tool:
        raise ApiError(400, "bad_request", "仅为工具卡添加工具项")

    # Override card_id from path
    payload.card_id = card_id
    try:
        item = create_tool_item(db, context, payload=payload)
    except ValueError as exc:
        raise ApiError(400, "bad_request", str(exc)) from exc

    return envelope(
        jsonable_encoder(item),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.get("/{card_id}/tools", response_model=ResponseEnvelope[list[AgentToolItemRead]])
def list_tool_items_route(
    card_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    _auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    card = get_card(db, card_id)
    if card is None:
        raise ApiError(404, "bad_request", f"卡牌未找到: {card_id}")

    items = list_tool_items(db, card_id)
    return envelope(
        jsonable_encoder(items),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.get("/{card_id}/tools/{item_id}", response_model=ResponseEnvelope[AgentToolItemRead])
def get_tool_item_route(
    card_id: UUID,
    item_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    _auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    item = get_tool_item(db, item_id)
    if item is None or str(item.card_id) != str(card_id):
        raise ApiError(404, "bad_request", f"工具项未找到: {item_id}")

    return envelope(
        jsonable_encoder(item),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.patch("/{card_id}/tools/{item_id}", response_model=ResponseEnvelope[AgentToolItemRead])
def update_tool_item_route(
    card_id: UUID,
    item_id: UUID,
    payload: AgentToolItemUpdateRequest,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    context = _wire_actor(context, auth)
    item = update_tool_item(db, context, item_id=item_id, payload=payload)
    if item is None:
        raise ApiError(404, "bad_request", f"工具项未找到: {item_id}")

    return envelope(
        jsonable_encoder(item),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.delete("/{card_id}/tools/{item_id}", response_model=ResponseEnvelope[dict])
def archive_tool_item_route(
    card_id: UUID,
    item_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    context = _wire_actor(context, auth)
    ok = archive_tool_item(db, context, item_id=item_id)
    if not ok:
        raise ApiError(404, "bad_request", f"工具项未找到或已归档: {item_id}")

    return envelope(
        jsonable_encoder({"deleted": True, "item_id": str(item_id)}),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )
