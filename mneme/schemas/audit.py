from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import Field

from mneme.schemas.common import ActorRef, ApiSchema, SensitivityLevel


class AuditResult(str, Enum):
    success = "success"
    denied = "denied"
    failed = "failed"


class AuditEventRead(ApiSchema):
    audit_id: UUID
    occurred_at: datetime
    actor: ActorRef
    action: str = Field(min_length=1, max_length=120)
    object_type: str | None = Field(default=None, max_length=80)
    object_id: UUID | None = None
    project_id: UUID | None = None
    result: AuditResult
    reason_code: str | None = Field(default=None, max_length=80)
    sensitivity_level: SensitivityLevel
    correlation_id: UUID
    request_id: UUID
    review_item_id: UUID | None = None
    diff_summary: dict[str, Any] = Field(default_factory=dict)
    metadata_json: dict[str, Any] = Field(default_factory=dict)


class AuditEventCreate(ApiSchema):
    action: str = Field(min_length=1, max_length=120)
    object_type: str | None = Field(default=None, max_length=80)
    object_id: UUID | None = None
    project_id: UUID | None = None
    result: AuditResult = AuditResult.success
    reason_code: str | None = Field(default=None, max_length=80)
    sensitivity_level: SensitivityLevel = SensitivityLevel.normal
    review_item_id: UUID | None = None
    diff_summary: dict[str, Any] = Field(default_factory=dict)
    metadata_json: dict[str, Any] = Field(default_factory=dict)

