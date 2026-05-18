from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import Field, SecretStr, model_validator

from mneme.schemas.common import ApiSchema, PaginatedData, SensitivityLevel


class AgentStatus(str, Enum):
    active = "active"
    disabled = "disabled"
    archived = "archived"


class AgentCreateRequest(ApiSchema):
    agent_code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    project_id: UUID | None = None
    store_id: UUID | None = None
    model_id: UUID | None = None
    sensitivity_ceiling: SensitivityLevel = SensitivityLevel.normal
    policy_json: dict[str, Any] = Field(default_factory=dict)


class AgentUpdateRequest(ApiSchema):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    project_id: UUID | None = None
    store_id: UUID | None = None
    model_id: UUID | None = None
    sensitivity_ceiling: SensitivityLevel | None = None
    policy_json: dict[str, Any] | None = None


class AgentRead(ApiSchema):
    agent_id: UUID
    project_id: UUID | None = None
    agent_code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    status: AgentStatus
    owner_user_id: UUID | None = None
    store_id: UUID | None = None
    model_id: UUID | None = None
    sensitivity_ceiling: SensitivityLevel
    policy_json: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    disabled_at: datetime | None = None


class AgentTokenRead(ApiSchema):
    token_id: UUID
    agent_id: UUID
    issued_by_user_id: UUID
    name: str | None = None
    token_prefix: str = Field(min_length=1, max_length=24)
    token_fingerprint: str = Field(min_length=1, max_length=128)
    project_scope: list[str] = Field(default_factory=list)
    capability_scope: list[str] = Field(default_factory=list)
    scopes: list[str] = Field(default_factory=list)
    sensitivity_ceiling: SensitivityLevel
    budget_limit_daily: Decimal | None = None
    rate_limit_per_min: int | None = Field(default=None, ge=1)
    expires_at: datetime
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class AgentTokenCreateRequest(ApiSchema):
    # ── Frontend format ──
    name: str | None = Field(default=None, min_length=1, max_length=128)
    scopes: list[str] = Field(default_factory=list)
    expires_in_days: int | None = Field(default=None, ge=1)

    # ── Backend format (backward compatible) ──
    project_scope: list[str] = Field(default_factory=list)
    capability_scope: list[str] = Field(default_factory=list)
    sensitivity_ceiling: SensitivityLevel = SensitivityLevel.normal
    budget_limit_daily: Decimal | None = Field(default=None, ge=0)
    rate_limit_per_min: int | None = Field(default=None, ge=1)
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def _compute_expires_at(self) -> "AgentTokenCreateRequest":
        if self.expires_at is None:
            if self.expires_in_days is not None:
                self.expires_at = datetime.now(timezone.utc) + timedelta(
                    days=self.expires_in_days
                )
            else:
                raise ValueError("expires_at or expires_in_days must be provided")
        return self

    @model_validator(mode="after")
    def _merge_scopes(self) -> "AgentTokenCreateRequest":
        # If frontend scopes are provided and capability_scope is empty,
        # merge scopes into capability_scope.
        if self.scopes and not self.capability_scope:
            self.capability_scope = list(self.scopes)
        return self


class AgentTokenCreateResponse(ApiSchema):
    token_id: UUID
    agent_id: UUID
    name: str | None = None
    token_raw: str = Field(description="Returned only once at token creation; only a hash is stored.")
    token_prefix: str = Field(min_length=1, max_length=24)
    scopes: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None
    created_at: datetime


class AgentTokenRevokeRequest(ApiSchema):
    revoke_reason: str | None = Field(default=None, max_length=64)


class AgentTokenRevokeResponse(ApiSchema):
    token_id: UUID
    revoked_at: datetime


class AgentListResponse(PaginatedData[AgentRead]):
    pass
