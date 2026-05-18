"""P2-08/P2-09 Vault API schemas for ``credential_vault`` and ``vault_access_logs``.

Schema alignment
----------------
All enumerations and field names match the DDL CHECK constraints defined in
``0001_baseline_45_tables.py``:

* ``credential_type`` — 4 values (api_key, oauth, cert, secret)
* ``status`` — 4 values (active, disabled, rotated, revoked)
* ``action`` (access log) — 8 values (create, enable, disable, rotate, revoke, export, use, access_denied)
* ``result`` (access log) — 3 values (success, denied, failed)
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import Field

from mneme.schemas.common import ApiSchema, PageInfo, PaginatedData, PaginationParams


# ═══════════════════════════════════════════════════════════════════════════════
# Enums (aligned with DDL CHECK constraints)
# ═══════════════════════════════════════════════════════════════════════════════


class CredentialType(str, Enum):
    """``credential_vault.credential_type`` CHECK constraint values."""

    api_key = "api_key"
    oauth = "oauth"
    cert = "cert"
    secret = "secret"


class CredentialStatus(str, Enum):
    """``credential_vault.status`` CHECK constraint values."""

    active = "active"
    disabled = "disabled"
    rotated = "rotated"
    revoked = "revoked"


class VaultAccessAction(str, Enum):
    """``vault_access_logs.action`` CHECK constraint values."""

    create = "create"
    enable = "enable"
    disable = "disable"
    rotate = "rotate"
    revoke = "revoke"
    export = "export"
    use = "use"
    access_denied = "access_denied"


class VaultAccessResult(str, Enum):
    """``vault_access_logs.result`` CHECK constraint values."""

    success = "success"
    denied = "denied"
    failed = "failed"


# ═══════════════════════════════════════════════════════════════════════════════
# Filter / pagination
# ═══════════════════════════════════════════════════════════════════════════════


class CredentialFilterParams(ApiSchema):
    """Query-string filters for ``GET /vault/credentials``."""

    provider_id: UUID | None = None
    credential_type: CredentialType | None = None
    status: CredentialStatus | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None


class VaultAccessLogFilterParams(ApiSchema):
    """Query-string filters for ``GET /vault/credentials/{id}/access-logs``."""

    action: VaultAccessAction | None = None
    result: VaultAccessResult | None = None
    occurred_after: datetime | None = None
    occurred_before: datetime | None = None


# ═══════════════════════════════════════════════════════════════════════════════
# Create model
# ═══════════════════════════════════════════════════════════════════════════════


class CredentialCreate(ApiSchema):
    """Request body for ``POST /vault/credentials``.

    The ``plaintext`` field accepts the raw credential value.  It is
    immediately encrypted and NEVER stored or logged in plaintext.
    """

    provider_id: UUID
    credential_name: str = Field(min_length=1, max_length=120)
    credential_type: CredentialType
    plaintext: str = Field(min_length=1, description="要加密的原始凭据值")
    scope_json: dict[str, Any] = Field(
        default_factory=dict,
        description="Scope restrictions, e.g. {capability_ids: [...], project_id: ...}",
    )
    metadata_json: dict[str, Any] = Field(default_factory=dict)


class CredentialUpdate(ApiSchema):
    """``PUT /vault/credentials/{id}`` 的请求体（轮换）。

    至少需提供 ``plaintext``、``status``、``scope_json`` 或
    ``metadata_json`` 其中之一。
    """

    plaintext: str | None = Field(
        default=None,
        min_length=1,
        description="新的凭据值（触发使用新 DEK 重新加密）",
    )
    status: CredentialStatus | None = None
    scope_json: dict[str, Any] | None = None
    metadata_json: dict[str, Any] | None = None


class CredentialRevealRequest(ApiSchema):
    """``POST /vault/credentials/{id}/reveal`` 的请求体。

    ``reason`` 字段记录在审计日志和访问日志中。

    当查看操作受审核控制时（P2-10），首次调用将返回 202 并附带
    ``review_item_id``。审核批准后，调用方可重新调用
    reveal 并设置 ``review_item_id`` 为已批准审核项的ID，以
    获取明文。
    """

    reason: str | None = Field(
        default=None,
        description="查看凭据明文的理由",
    )
    review_item_id: UUID | None = Field(
        default=None,
        description="来自先前查看请求的已批准 review_item_id。 "
        "当提供且审核已批准时，将跳过策略检查。",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Read models (NEVER include plaintext except in reveal response)
# ═══════════════════════════════════════════════════════════════════════════════


class CredentialRead(ApiSchema):
    """A ``credential_vault`` row returned by list/detail endpoints.

    **No plaintext is ever included.**  The ``ciphertext`` and ``key_wrap``
    fields are omitted entirely from API responses.
    """

    credential_id: UUID
    provider_id: UUID
    credential_name: str
    credential_type: str
    status: str
    key_version: str
    fingerprint: str
    scope_json: dict[str, Any] = Field(default_factory=dict)
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    rotated_at: datetime | None = None
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None
    created_by_user_id: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class CredentialRevealResponse(ApiSchema):
    """Response for ``POST /vault/credentials/{id}/reveal``.

    Contains the decrypted plaintext.  This is a **highly sensitive**
    endpoint that must be protected by step-up auth and audit logging.
    """

    credential_id: UUID
    credential_name: str
    credential_type: str
    plaintext: str
    fingerprint: str


class VaultAccessLogRead(ApiSchema):
    """A ``vault_access_logs`` row returned by the access log API.

    **No plaintext credentials are ever included.**
    """

    access_log_id: UUID
    credential_id: UUID | None = None
    actor_type: str
    actor_id: UUID | None = None
    auth_context_type: str | None = None
    auth_context_id: UUID | None = None
    action: str
    result: str
    capability_id: UUID | None = None
    provider_id: UUID | None = None
    request_id: UUID | None = None
    correlation_id: UUID | None = None
    reason_code: str | None = None
    target_scope: dict[str, Any] = Field(default_factory=dict)
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime | None = None


# ═══════════════════════════════════════════════════════════════════════════════
# List responses
# ═══════════════════════════════════════════════════════════════════════════════


class CredentialListResponse(PaginatedData[CredentialRead]):
    """Paginated list of credentials."""
    pass


class VaultAccessLogListResponse(PaginatedData[VaultAccessLogRead]):
    """Paginated list of vault access logs."""
    pass
