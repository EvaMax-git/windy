from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import Field, SecretStr

from mneme.schemas.common import ApiSchema


class UserRole(str, Enum):
    owner = "owner"
    operator = "operator"
    viewer = "viewer"
    auditor = "auditor"


class UserStatus(str, Enum):
    pending_bootstrap = "pending_bootstrap"
    active = "active"
    disabled = "disabled"
    locked = "locked"


class MfaMode(str, Enum):
    none = "none"
    totp = "totp"
    passkey = "passkey"
    required_but_unbound = "required_but_unbound"


class SessionAuthMethod(str, Enum):
    password = "password"
    password_totp = "password_totp"
    passkey = "passkey"
    bootstrap = "bootstrap"


class UserRead(ApiSchema):
    user_id: UUID
    username: str = Field(min_length=1, max_length=80)
    email: str | None = Field(default=None, max_length=255)
    display_name: str = Field(min_length=1, max_length=120)
    role_code: UserRole
    status: UserStatus
    mfa_mode: MfaMode
    locale: str = Field(min_length=1, max_length=32)
    timezone: str = Field(min_length=1, max_length=64)
    last_login_at: datetime | None = None
    disabled_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class UserSessionRead(ApiSchema):
    session_id: UUID
    user_id: UUID
    session_token_prefix: str = Field(min_length=1, max_length=24)
    auth_method: SessionAuthMethod
    device_label: str | None = Field(default=None, max_length=200)
    step_up_verified_at: datetime | None = None
    last_seen_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    revoke_reason: str | None = Field(default=None, max_length=64)
    created_at: datetime
    updated_at: datetime


class LoginRequest(ApiSchema):
    username: str = Field(min_length=1, max_length=255)
    password: SecretStr = Field(min_length=1)
    totp_code: str | None = Field(default=None, min_length=6, max_length=12)
    device_label: str | None = Field(default=None, max_length=200)


class LoginResponse(ApiSchema):
    user: UserRead
    session: UserSessionRead
    # NOTE: session_token is deliberately excluded from the response body.
    # The token is transmitted exclusively via Set-Cookie (HttpOnly, Secure,
    # SameSite=Strict) to prevent leakage through browser extensions, proxy
    # logs, frontend error tracking, and XSS-sniffable JavaScript access.


class LogoutRequest(ApiSchema):
    revoke_reason: str | None = Field(default=None, max_length=64)


class LogoutResponse(ApiSchema):
    session_id: UUID
    revoked_at: datetime


class MeResponse(ApiSchema):
    user: UserRead
    session: UserSessionRead
