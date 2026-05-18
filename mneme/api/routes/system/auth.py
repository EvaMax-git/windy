from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session

from mneme.api.auth import get_current_user_session
from mneme.api.context import RequestContext, get_request_context
from mneme.api.errors import ApiError
from mneme.api.schemas import envelope
from mneme.config import get_settings
from mneme.db.auth import (
    AuthenticatedSession,
    RequestAuthMetadata,
    login_user,
    logout_session,
)
from mneme.db.base import get_db
from mneme.schemas import (
    LoginRequest,
    LoginResponse,
    LogoutRequest,
    LogoutResponse,
    MeResponse,
    ResponseEnvelope,
)


router = APIRouter(prefix="/auth", tags=["auth"])


def _request_auth_metadata(request: Request) -> RequestAuthMetadata:
    return RequestAuthMetadata(
        user_agent=request.headers.get("User-Agent"),
        client_host=request.client.host if request.client is not None else None,
    )


@router.post("/login", response_model=ResponseEnvelope[LoginResponse])
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    settings = get_settings()
    result = login_user(
        db,
        context,
        username=payload.username,
        password=payload.password.get_secret_value(),
        device_label=payload.device_label,
        metadata=_request_auth_metadata(request),
        ttl_hours=settings.session_ttl_hours,
    )
    if result is None:
        raise ApiError(401, "auth_required", "用户名或密码错误")

    max_age = settings.session_ttl_hours * 60 * 60
    response.set_cookie(
        settings.session_cookie_name,
        result.session_token,
        max_age=max_age,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/api/v4",
    )

    data = LoginResponse(
        user=result.user,
        session=result.session,
    )
    return envelope(
        jsonable_encoder(data),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.post("/logout", response_model=ResponseEnvelope[LogoutResponse])
def logout(
    response: Response,
    payload: LogoutRequest = LogoutRequest(),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    revoked_at = logout_session(
        db,
        context,
        auth=auth,
        revoke_reason=payload.revoke_reason,
    )
    settings = get_settings()
    response.delete_cookie(settings.session_cookie_name, path="/api/v4", samesite="lax")

    data = LogoutResponse(session_id=auth.session.session_id, revoked_at=revoked_at)
    return envelope(
        jsonable_encoder(data),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.get("/me", response_model=ResponseEnvelope[MeResponse])
def me(
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    data = MeResponse(user=auth.user, session=auth.session)
    return envelope(
        jsonable_encoder(data),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )
