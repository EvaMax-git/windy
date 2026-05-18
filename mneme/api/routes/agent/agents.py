from __future__ import annotations

from math import ceil
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
from mneme.db.agents import (
    archive_agent,
    create_agent,
    create_agent_token,
    disable_agent,
    get_agent,
    list_agent_tokens,
    list_agents,
    revoke_agent_token,
    update_agent,
)
from mneme.db.auth import AuthenticatedSession
from mneme.db.base import get_db
from mneme.db.audit import add_audit_event
from mneme.schemas import (
    AgentCreateRequest,
    AgentListResponse,
    AgentRead,
    AgentTokenCreateRequest,
    AgentTokenCreateResponse,
    AgentTokenRead,
    AgentTokenRevokeRequest,
    AgentTokenRevokeResponse,
    AgentUpdateRequest,
    PageInfo,
    ResponseEnvelope,
)
from mneme.security import (
    Action,
    Object,
    PolicyContext,
    actor_from_user_session,
    can,
)
from mneme.security.audit import audit_event_for_policy_denied

router = APIRouter(prefix="/agents", tags=["agents"])


def _check_policy(
    *,
    auth: AuthenticatedSession,
    action_name: str,
    object_type: str,
    context: RequestContext,
    db: Session,
) -> None:
    """Check the Policy Engine and raise 403 on deny."""
    policy_actor = actor_from_user_session(
        user_id=auth.user.user_id,
        role=auth.user.role_code.value,
        status=auth.user.status.value,
    )
    policy_action = Action(name=action_name)
    policy_object = Object(object_type=object_type)
    policy_ctx = PolicyContext(
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )

    decision = can(policy_actor, policy_action, policy_object, policy_ctx)
    if decision.decision.value != "allow":
        audit = audit_event_for_policy_denied(
            action=action_name,
            decision=decision,
            object_type=object_type,
        )
        add_audit_event(db, context, audit)
        raise ApiError(
            403,
            "permission_denied",
            decision.message or "权限不足",
            details=decision.details,
        )


def _wire_actor(
    context: RequestContext,
    auth: AuthenticatedSession,
) -> RequestContext:
    """Return a new RequestContext with the authenticated user as the actor."""
    return with_actor(
        context,
        actor=ActorContext(
            actor_type="user",
            actor_id=auth.user.user_id,
            auth_context_type="user_session",
            auth_context_id=auth.session.session_id,
        ),
    )


# ── Agent CRUD ───────────────────────────────────────────────────────────────

@router.post("", response_model=ResponseEnvelope[AgentRead], status_code=201)
def create_agent_route(
    payload: AgentCreateRequest,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    _check_policy(
        auth=auth,
        action_name="agent.create",
        object_type="agent",
        context=context,
        db=db,
    )
    context = _wire_actor(context, auth)

    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "写请求必须提供 Idempotency-Key 头")

    try:
        agent = create_agent(
            db,
            context,
            payload=payload,
            owner_user_id=auth.user.user_id,
        )
    except ValueError as exc:
        raise ApiError(409, "idempotency_conflict", str(exc)) from exc

    return envelope(
        jsonable_encoder(agent),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.get("", response_model=ResponseEnvelope[AgentListResponse])
def list_agents_route(
    page: int = 1,
    page_size: int = 50,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    _auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    agents, total = list_agents(db, page=page, page_size=page_size)

    data = AgentListResponse(
        items=agents,
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


@router.get("/{agent_id}", response_model=ResponseEnvelope[AgentRead])
def get_agent_route(
    agent_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    _auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    agent = get_agent(db, agent_id)
    if agent is None:
        raise ApiError(404, "bad_request", f"Agent 未找到: {agent_id}")

    return envelope(
        jsonable_encoder(agent),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ── Agent lifecycle ──────────────────────────────────────────────────────────

@router.patch("/{agent_id}", response_model=ResponseEnvelope[AgentRead])
def update_agent_route(
    agent_id: UUID,
    payload: AgentUpdateRequest,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    _check_policy(
        auth=auth,
        action_name="agent.update",
        object_type="agent",
        context=context,
        db=db,
    )
    context = _wire_actor(context, auth)

    if get_agent(db, agent_id) is None:
        raise ApiError(404, "bad_request", f"Agent not found: {agent_id}")

    try:
        agent = update_agent(db, context, agent_id=agent_id, payload=payload)
    except ValueError as exc:
        raise ApiError(400, "bad_request", str(exc)) from exc

    return envelope(
        jsonable_encoder(agent),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.post("/{agent_id}/disable", response_model=ResponseEnvelope[AgentRead])
def disable_agent_route(
    agent_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    _check_policy(
        auth=auth,
        action_name="agent.disable",
        object_type="agent",
        context=context,
        db=db,
    )
    context = _wire_actor(context, auth)

    if get_agent(db, agent_id) is None:
        raise ApiError(404, "bad_request", f"Agent not found: {agent_id}")

    try:
        agent = disable_agent(db, context, agent_id=agent_id)
    except ValueError as exc:
        raise ApiError(400, "bad_request", str(exc)) from exc

    return envelope(
        jsonable_encoder(agent),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.post("/{agent_id}/archive", response_model=ResponseEnvelope[AgentRead])
def archive_agent_route(
    agent_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    _check_policy(
        auth=auth,
        action_name="agent.archive",
        object_type="agent",
        context=context,
        db=db,
    )
    context = _wire_actor(context, auth)

    if get_agent(db, agent_id) is None:
        raise ApiError(404, "bad_request", f"Agent not found: {agent_id}")

    try:
        agent = archive_agent(db, context, agent_id=agent_id)
    except ValueError as exc:
        raise ApiError(400, "bad_request", str(exc)) from exc

    return envelope(
        jsonable_encoder(agent),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ── Agent Token management ───────────────────────────────────────────────────

@router.post(
    "/{agent_id}/tokens",
    response_model=ResponseEnvelope[AgentTokenCreateResponse],
    status_code=201,
)
def create_agent_token_route(
    agent_id: UUID,
    payload: AgentTokenCreateRequest,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    _check_policy(
        auth=auth,
        action_name="agent_token.create",
        object_type="agent_token",
        context=context,
        db=db,
    )
    context = _wire_actor(context, auth)

    agent = get_agent(db, agent_id)
    if agent is None:
        raise ApiError(404, "bad_request", f"Agent 未找到: {agent_id}")

    try:
        result = create_agent_token(
            db,
            context,
            agent_id=agent_id,
            payload=payload,
            issued_by_user_id=auth.user.user_id,
        )
    except ValueError as exc:
        raise ApiError(400, "bad_request", str(exc)) from exc

    token = result.token
    data = AgentTokenCreateResponse(
        token_id=token.token_id,
        agent_id=token.agent_id,
        name=token.name,
        token_raw=result.token_secret,
        token_prefix=token.token_prefix,
        scopes=token.scopes,
        expires_at=token.expires_at,
        created_at=token.created_at,
    )
    return envelope(
        jsonable_encoder(data),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.get(
    "/{agent_id}/tokens",
    response_model=ResponseEnvelope[list[AgentTokenRead]],
)
def list_agent_tokens_route(
    agent_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    _auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    agent = get_agent(db, agent_id)
    if agent is None:
        raise ApiError(404, "bad_request", f"Agent 未找到: {agent_id}")

    tokens = list_agent_tokens(db, agent_id)
    return envelope(
        jsonable_encoder(tokens),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.post(
    "/{agent_id}/tokens/{token_id}/revoke",
    response_model=ResponseEnvelope[AgentTokenRevokeResponse],
)
def revoke_agent_token_route(
    agent_id: UUID,
    token_id: UUID,
    payload: AgentTokenRevokeRequest,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    _check_policy(
        auth=auth,
        action_name="agent_token.revoke",
        object_type="agent_token",
        context=context,
        db=db,
    )
    context = _wire_actor(context, auth)

    agent = get_agent(db, agent_id)
    if agent is None:
        raise ApiError(404, "bad_request", f"Agent 未找到: {agent_id}")

    revoked_at = revoke_agent_token(
        db,
        context,
        agent_id=agent_id,
        token_id=token_id,
        revoke_reason=payload.revoke_reason or "manual_revoke",
    )

    if revoked_at is None:
        raise ApiError(404, "bad_request", f"活跃令牌未找到: {token_id}")

    return envelope(
        jsonable_encoder(
            AgentTokenRevokeResponse(token_id=token_id, revoked_at=revoked_at)
        ),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )
