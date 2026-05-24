"""API Key authentication for external consumers (A5 MVP).

Accepts authentication via:
1. Session cookie (web users)
2. Authorization: Bearer <agent_token> (agents)
3. X-API-Key: <api_key> (external systems — maps to agent tokens)

This dependency is used on public-facing endpoints that need to support
both web users and external API consumers.
"""

from __future__ import annotations

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from mneme.api.errors import ApiError
from mneme.db.agents import AuthenticatedAgent, authenticate_agent_token
from mneme.db.auth import AuthenticatedSession, authenticate_session
from mneme.db.base import get_db


def get_api_consumer(
    request: Request,
    db: Session = Depends(get_db),
) -> AuthenticatedSession | AuthenticatedAgent:
    """Authenticate an API consumer via session, Bearer token, or X-API-Key.

    Returns either an AuthenticatedSession or AuthenticatedAgent.
    """
    from mneme.config import get_settings
    settings = get_settings()

    # 1. Try session cookie
    session_token = request.cookies.get(settings.session_cookie_name)
    if session_token:
        auth = authenticate_session(db, session_token)
        if auth is not None:
            return auth

    # 2. Try Authorization: Bearer <token>
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        if token:
            auth = authenticate_agent_token(db, token)
            if auth is not None:
                return auth

    # 3. Try X-API-Key header
    api_key = request.headers.get("X-API-Key")
    if api_key:
        auth = authenticate_agent_token(db, api_key)
        if auth is not None:
            return auth

    raise ApiError(401, "auth_required", "需要身份验证（支持 Session、Bearer Token 或 X-API-Key）")
