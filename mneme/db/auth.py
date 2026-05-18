from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session

from mneme.api.context import ActorContext, RequestContext
from mneme.config import get_settings
from mneme.db.audit import AuditEvent, add_audit_event
from mneme.db.transactions import session_scope, transaction
from mneme.schemas.auth import UserRead, UserSessionRead
from mneme.security import (
    generate_session_token,
    hash_optional_fingerprint,
    hash_password,
    hash_session_token,
    verify_password,
)


_DUMMY_PASSWORD_HASH = hash_password("mneme-invalid-password")


@dataclass(frozen=True)
class RequestAuthMetadata:
    user_agent: str | None = None
    client_host: str | None = None

    @property
    def ip_hash(self) -> str | None:
        return hash_optional_fingerprint(self.client_host)


@dataclass(frozen=True)
class LoginResult:
    user: UserRead
    session: UserSessionRead
    session_token: str


@dataclass(frozen=True)
class AuthenticatedSession:
    user: UserRead
    session: UserSessionRead
    session_token_hash: str


_SELECT_USER_BY_IDENTIFIER = text(
    """
    SELECT
      user_id,
      username,
      email,
      display_name,
      role_code,
      status,
      password_hash,
      mfa_mode,
      locale,
      timezone,
      last_login_at,
      disabled_at,
      created_at,
      updated_at
    FROM users
    WHERE username = :identifier OR email = :identifier
    LIMIT 1
    """
)

_INSERT_SESSION = text(
    """
    INSERT INTO user_sessions (
      session_id,
      user_id,
      session_token_hash,
      session_token_prefix,
      auth_method,
      device_label,
      ip_hash,
      user_agent,
      expires_at
    )
    VALUES (
      :session_id,
      :user_id,
      :session_token_hash,
      :session_token_prefix,
      'password',
      :device_label,
      :ip_hash,
      :user_agent,
      :expires_at
    )
    RETURNING
      session_id,
      user_id,
      session_token_prefix,
      auth_method,
      device_label,
      step_up_verified_at,
      last_seen_at,
      expires_at,
      revoked_at,
      revoke_reason,
      created_at,
      updated_at
    """
).bindparams(
    bindparam("session_id", type_=PG_UUID(as_uuid=True)),
    bindparam("user_id", type_=PG_UUID(as_uuid=True)),
)

_TOUCH_SESSION_AND_USER = text(
    """
    UPDATE user_sessions
    SET last_seen_at = :now
    WHERE session_token_hash = :session_token_hash
      AND revoked_at IS NULL
      AND expires_at > :now
      AND EXISTS (
        SELECT 1
        FROM users
        WHERE users.user_id = user_sessions.user_id
          AND users.status = 'active'
      )
    RETURNING
      session_id,
      user_id,
      session_token_hash,
      session_token_prefix,
      auth_method,
      device_label,
      step_up_verified_at,
      last_seen_at,
      expires_at,
      revoked_at,
      revoke_reason,
      created_at,
      updated_at
    """
)

_SELECT_USER_BY_ID = text(
    """
    SELECT
      user_id,
      username,
      email,
      display_name,
      role_code,
      status,
      mfa_mode,
      locale,
      timezone,
      last_login_at,
      disabled_at,
      created_at,
      updated_at
    FROM users
    WHERE user_id = :user_id
    """
).bindparams(bindparam("user_id", type_=PG_UUID(as_uuid=True)))

_REVOKE_SESSION = text(
    """
    UPDATE user_sessions
    SET revoked_at = :revoked_at,
        revoke_reason = :revoke_reason
    WHERE session_id = :session_id
      AND revoked_at IS NULL
    RETURNING revoked_at
    """
).bindparams(bindparam("session_id", type_=PG_UUID(as_uuid=True)))

_INSERT_BOOTSTRAP_OWNER = text(
    """
    INSERT INTO users (
      user_id,
      username,
      email,
      display_name,
      role_code,
      status,
      password_hash
    )
    VALUES (
      :user_id,
      :username,
      :email,
      :display_name,
      'owner',
      'active',
      :password_hash
    )
    """
).bindparams(bindparam("user_id", type_=PG_UUID(as_uuid=True)))


def _auth_context(context: RequestContext, user_id: UUID | None, session_id: UUID | None) -> RequestContext:
    return RequestContext(
        request_id=context.request_id,
        correlation_id=context.correlation_id,
        idempotency_key=context.idempotency_key,
        actor=ActorContext(
            actor_type="user" if user_id is not None else "system",
            actor_id=user_id,
            auth_context_type="user_session" if session_id is not None else None,
            auth_context_id=session_id,
        ),
    )


def _mapping(row: Any) -> dict[str, Any]:
    return dict(row._mapping)


def _as_uuid(value: Any) -> UUID:
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def _user_from_row(row: Any) -> UserRead:
    data = _mapping(row)
    data.pop("password_hash", None)
    return UserRead.model_validate(data)


def _session_from_row(row: Any) -> UserSessionRead:
    data = _mapping(row)
    data.pop("session_token_hash", None)
    return UserSessionRead.model_validate(data)


def _record_login_audit(
    db: Session,
    context: RequestContext,
    *,
    result: str,
    user_id: UUID | None = None,
    session_id: UUID | None = None,
    reason_code: str | None = None,
    metadata_json: dict[str, Any] | None = None,
) -> None:
    audit_context = _auth_context(context, user_id, session_id)
    add_audit_event(
        db,
        audit_context,
        AuditEvent(
            action="auth.login",
            result=result,
            object_type="user_session" if session_id is not None else None,
            object_id=session_id,
            reason_code=reason_code,
            metadata_json=metadata_json or {},
        ),
    )


def _record_logout_audit(
    db: Session,
    context: RequestContext,
    *,
    user_id: UUID,
    session_id: UUID,
    revoke_reason: str,
) -> None:
    add_audit_event(
        db,
        _auth_context(context, user_id, session_id),
        AuditEvent(
            action="auth.logout",
            result="success",
            object_type="user_session",
            object_id=session_id,
            reason_code=revoke_reason,
        ),
    )


def login_user(
    db: Session,
    context: RequestContext,
    *,
    username: str,
    password: str,
    device_label: str | None,
    metadata: RequestAuthMetadata,
    ttl_hours: int,
) -> LoginResult | None:
    now = datetime.now(timezone.utc)
    user_row = db.execute(_SELECT_USER_BY_IDENTIFIER, {"identifier": username}).first()
    stored_password_hash = user_row._mapping["password_hash"] if user_row is not None else _DUMMY_PASSWORD_HASH
    password_ok = verify_password(password, stored_password_hash)

    if user_row is None or not password_ok:
        with transaction(db):
            _record_login_audit(
                db,
                context,
                result="failed",
                reason_code="invalid_credentials",
                metadata_json={"user_found": user_row is not None},
            )
        return None

    user_data = _mapping(user_row)
    user_id = _as_uuid(user_data["user_id"])
    user_data["user_id"] = user_id
    if user_data["status"] != "active":
        with transaction(db):
            _record_login_audit(
                db,
                context,
                result="denied",
                user_id=user_id,
                reason_code=f"user_{user_data['status']}",
            )
        return None

    session_token = generate_session_token()
    session_id = uuid4()
    session_token_hash = hash_session_token(session_token)
    expires_at = now + timedelta(hours=ttl_hours)

    with transaction(db):
        session_row = db.execute(
            _INSERT_SESSION,
            {
                "session_id": session_id,
                "user_id": user_id,
                "session_token_hash": session_token_hash,
                "session_token_prefix": session_token[:12],
                "device_label": device_label,
                "ip_hash": metadata.ip_hash,
                "user_agent": metadata.user_agent,
                "expires_at": expires_at,
            },
        ).one()
        db.execute(
            text("UPDATE users SET last_login_at = :now WHERE user_id = :user_id").bindparams(
                bindparam("user_id", type_=PG_UUID(as_uuid=True))
            ),
            {"now": now, "user_id": user_id},
        )
        _record_login_audit(
            db,
            context,
            result="success",
            user_id=user_id,
            session_id=session_id,
        )

    user_data["last_login_at"] = now
    return LoginResult(
        user=UserRead.model_validate({k: v for k, v in user_data.items() if k != "password_hash"}),
        session=_session_from_row(session_row),
        session_token=session_token,
    )


def authenticate_session(db: Session, token: str) -> AuthenticatedSession | None:
    now = datetime.now(timezone.utc)
    token_hash = hash_session_token(token)

    with transaction(db):
        session_row = db.execute(
            _TOUCH_SESSION_AND_USER,
            {"session_token_hash": token_hash, "now": now},
        ).first()
        if session_row is None:
            return None

        user_row = db.execute(_SELECT_USER_BY_ID, {"user_id": _as_uuid(session_row._mapping["user_id"])}).first()
        if user_row is None:
            return None

    return AuthenticatedSession(
        user=_user_from_row(user_row),
        session=_session_from_row(session_row),
        session_token_hash=token_hash,
    )


def logout_session(
    db: Session,
    context: RequestContext,
    *,
    auth: AuthenticatedSession,
    revoke_reason: str | None,
) -> datetime:
    now = datetime.now(timezone.utc)
    reason = revoke_reason or "user_logout"

    with transaction(db):
        revoked_at = db.execute(
            _REVOKE_SESSION,
            {
                "session_id": auth.session.session_id,
                "revoked_at": now,
                "revoke_reason": reason,
            },
        ).scalar_one_or_none()
        _record_logout_audit(
            db,
            context,
            user_id=auth.user.user_id,
            session_id=auth.session.session_id,
            revoke_reason=reason,
        )

    return revoked_at or now


def bootstrap_owner_if_configured() -> bool:
    settings = get_settings()
    if settings.bootstrap_owner_password is None:
        return False

    with session_scope() as db:
        existing_count = db.execute(text("SELECT count(*) FROM users")).scalar_one()
        if existing_count:
            return False

        db.execute(
            _INSERT_BOOTSTRAP_OWNER,
            {
                "user_id": uuid4(),
                "username": settings.bootstrap_owner_username,
                "email": settings.bootstrap_owner_email,
                "display_name": settings.bootstrap_owner_username,
                "password_hash": hash_password(settings.bootstrap_owner_password.get_secret_value()),
            },
        )
    return True
