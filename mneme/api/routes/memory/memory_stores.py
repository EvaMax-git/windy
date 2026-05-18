from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session

from mneme.api.auth import (
    extract_agent_token,
    get_current_agent,
    get_current_user_session,
)
from mneme.api.context import RequestContext, get_request_context
from mneme.api.errors import ApiError
from mneme.api.schemas import envelope
from mneme.db.agents import AuthenticatedAgent
from mneme.db.auth import AuthenticatedSession
from mneme.db.base import get_db
from mneme.db.memory_stores import (
    bind_store_to_agent,
    create_store,
    delete_store,
    get_store,
    get_store_by_agent,
    list_stores,
    unbind_store,
    update_store,
)
from mneme.schemas.memory_stores import (
    MemoryStoreCreateRequest,
    MemoryStoreRead,
    MemoryStoreUpdateRequest,
)
from mneme.schemas.common import ResponseEnvelope

router = APIRouter(prefix="/memory-stores", tags=["memory_stores"])

# ── Helper: resolve optional agent (None if user session) ────────────────────


def _maybe_agent(request: Request, db: Session) -> AuthenticatedAgent | None:
    """Return the authenticated agent if the request uses a Bearer token."""
    token = extract_agent_token(request)
    if token is None:
        return None
    from mneme.db.agents import authenticate_agent_token
    return authenticate_agent_token(db, token)


# ── Routes ───────────────────────────────────────────────────────────────────


@router.post("", response_model=ResponseEnvelope[MemoryStoreRead], status_code=201)
def create_store_route(
    payload: MemoryStoreCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    _auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "Idempotency-Key header is required for writes")

    # Agent isolation: an agent can only create a store bound to itself.
    agent = _maybe_agent(request, db)
    if agent is not None:
        if payload.agent_id is not None and payload.agent_id != agent.agent.agent_id:
            raise ApiError(
                403, "permission_denied",
                f"Agent '{agent.agent.agent_code}' cannot create a store for another agent",
            )
        # Force the store to be bound to this agent
        payload.agent_id = agent.agent.agent_id

    try:
        store = create_store(db, payload=payload, context=context)
    except ValueError as exc:
        raise ApiError(400, "bad_request", str(exc)) from exc

    return envelope(
        jsonable_encoder(store),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.get("", response_model=ResponseEnvelope[list[MemoryStoreRead]])
def list_stores_route(
    agent_id: UUID | None = None,
    unbound_only: bool = False,
    request: Request = None,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    _auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    # Agent isolation: agents can only list their own stores.
    agent = _maybe_agent(request, db)
    if agent is not None:
        # Override agent_id to force scoping to the calling agent
        agent_id = agent.agent.agent_id
        unbound_only = False

    stores = list_stores(db, agent_id=agent_id, unbound_only=unbound_only)
    return envelope(
        jsonable_encoder(stores),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.get("/by-agent/{agent_id}", response_model=ResponseEnvelope[MemoryStoreRead | None])
def get_store_by_agent_route(
    agent_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    _auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    # Agent isolation: agents can only look up their own store
    agent = _maybe_agent(request, db)
    if agent is not None and agent_id != agent.agent.agent_id:
        raise ApiError(
            403, "permission_denied",
            f"Agent '{agent.agent.agent_code}' cannot look up stores for another agent",
        )

    store = get_store_by_agent(db, agent_id)
    return envelope(
        jsonable_encoder(store),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.get("/{store_id}", response_model=ResponseEnvelope[MemoryStoreRead])
def get_store_route(
    store_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    _auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    # check_store_access_middleware handles agent isolation
    store = get_store(db, store_id)
    if store is None:
        raise ApiError(404, "bad_request", f"MemoryStore 未找到: {store_id}")

    return envelope(
        jsonable_encoder(store),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.patch("/{store_id}", response_model=ResponseEnvelope[MemoryStoreRead])
def update_store_route(
    store_id: UUID,
    payload: MemoryStoreUpdateRequest,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    _auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    # check_store_access_middleware handles agent isolation
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "Idempotency-Key header is required for writes")
    if get_store(db, store_id) is None:
        raise ApiError(404, "bad_request", f"MemoryStore 未找到: {store_id}")

    try:
        store = update_store(db, store_id=store_id, payload=payload, context=context)
    except ValueError as exc:
        raise ApiError(400, "bad_request", str(exc)) from exc

    return envelope(
        jsonable_encoder(store),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.delete("/{store_id}", response_model=ResponseEnvelope[dict])
def delete_store_route(
    store_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    _auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    # check_store_access_middleware handles agent isolation
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "Idempotency-Key header is required for writes")
    deleted = delete_store(db, store_id=store_id)
    if not deleted:
        raise ApiError(404, "bad_request", f"MemoryStore 未找到: {store_id}")

    return envelope(
        jsonable_encoder({"deleted": True, "store_id": str(store_id)}),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.post("/{store_id}/bind/{agent_id}", response_model=ResponseEnvelope[MemoryStoreRead])
def bind_store_route(
    store_id: UUID,
    agent_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    _auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    # Only user sessions (admin) can bind stores. Agent cannot bind stores.
    agent = _maybe_agent(request, db)
    if agent is not None:
        raise ApiError(
            403, "permission_denied",
            "Only administrators can bind memory stores to agents",
        )

    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "Idempotency-Key header is required for writes")
    try:
        store = bind_store_to_agent(db, store_id=store_id, agent_id=agent_id)
    except ValueError as exc:
        raise ApiError(400, "bad_request", str(exc)) from exc

    return envelope(
        jsonable_encoder(store),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.post("/{store_id}/unbind", response_model=ResponseEnvelope[MemoryStoreRead])
def unbind_store_route(
    store_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    _auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    # Only user sessions (admin) can unbind stores. Agent cannot unbind stores.
    agent = _maybe_agent(request, db)
    if agent is not None:
        raise ApiError(
            403, "permission_denied",
            "Only administrators can unbind memory stores from agents",
        )

    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "Idempotency-Key header is required for writes")
    try:
        store = unbind_store(db, store_id=store_id)
    except ValueError as exc:
        raise ApiError(400, "bad_request", str(exc)) from exc

    return envelope(
        jsonable_encoder(store),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )
