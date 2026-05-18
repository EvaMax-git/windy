from __future__ import annotations

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from mneme.api.errors import ApiError
from mneme.config import get_settings
from mneme.db.agents import AuthenticatedAgent, authenticate_agent_token
from mneme.db.auth import AuthenticatedSession, authenticate_session
from mneme.db.base import get_db


AUTHORIZATION_HEADER_PREFIX = "Bearer "


def extract_session_token(request: Request) -> str | None:
    settings = get_settings()
    return request.cookies.get(settings.session_cookie_name)


def extract_agent_token(request: Request) -> str | None:
    authorization = request.headers.get("Authorization")
    if authorization and authorization.startswith(AUTHORIZATION_HEADER_PREFIX):
        token = authorization[len(AUTHORIZATION_HEADER_PREFIX) :].strip()
        return token or None
    return None


def get_current_user_session(
    request: Request,
    db: Session = Depends(get_db),
) -> AuthenticatedSession:
    token = extract_session_token(request)
    if token is None:
        raise ApiError(401, "auth_required", "需要身份验证")

    auth = authenticate_session(db, token)
    if auth is None:
        raise ApiError(401, "auth_required", "会话已过期或已吊销")

    return auth


def get_current_agent(
    request: Request,
    db: Session = Depends(get_db),
) -> AuthenticatedAgent:
    token = extract_agent_token(request)
    if token is None:
        raise ApiError(401, "auth_required", "需要 Agent 令牌")

    auth = authenticate_agent_token(db, token)
    if auth is None:
        raise ApiError(401, "auth_required", "Agent 令牌已过期、已吊销或无效")

    return auth
