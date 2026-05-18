"""Pydantic schemas for Context Compiler (P5-04)."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import Field

from mneme.schemas.common import ApiSchema, PaginatedData, SensitivityLevel


class CompileMode(str, Enum):
    full = "full"
    search_fallback = "search_fallback"


class ContextPackStatus(str, Enum):
    created = "created"
    used = "used"
    failed = "failed"
    expired = "expired"


class ContextItemType(str, Enum):
    knowledge_document = "knowledge_document"
    knowledge_block = "knowledge_block"
    knowledge_chunk = "knowledge_chunk"
    memory = "memory"
    raw_event = "raw_event"
    fallback_query = "fallback_query"


class TokenBudget(ApiSchema):
    max_tokens: int = Field(default=4096, ge=1, le=1000000)
    reserve_for_output: int = Field(default=512, ge=0)
    knowledge_ratio: float = Field(default=0.6, ge=0.0, le=1.0)
    memory_ratio: float = Field(default=0.4, ge=0.0, le=1.0)


class CompileRequest(ApiSchema):
    agent_id: UUID | None = None
    project_id: UUID | None = None
    query_text: str = Field(min_length=1, max_length=10000)
    compile_mode: CompileMode = CompileMode.full
    token_budget: TokenBudget = Field(default_factory=TokenBudget)
    sensitivity_ceiling: SensitivityLevel = SensitivityLevel.private


class ContextPackItemRead(ApiSchema):
    context_pack_item_id: UUID
    context_pack_id: UUID
    item_order: int
    item_type: ContextItemType
    object_id: UUID | None = None
    object_version: int | None = None
    source_ref: dict[str, Any] = Field(default_factory=dict)
    included: bool = True
    exclusion_reason: str | None = None
    score: float | None = None
    token_count: int | None = None
    reason: str | None = None
    content_digest: str | None = None
    created_at: datetime


class ContextPackRead(ApiSchema):
    context_pack_id: UUID
    request_id: UUID
    correlation_id: UUID
    agent_id: UUID | None = None
    project_id: UUID | None = None
    actor_type: str
    actor_id: UUID | None = None
    compile_mode: CompileMode
    status: ContextPackStatus
    knowledge_version_set: list[dict[str, Any]] = Field(default_factory=list)
    memory_version_set: list[dict[str, Any]] = Field(default_factory=list)
    token_budget: dict[str, Any] = Field(default_factory=dict)
    exclusion_summary: dict[str, Any] = Field(default_factory=dict)
    api_call_log_id: UUID | None = None
    retention_until: datetime
    created_at: datetime


class ContextPackDetailRead(ContextPackRead):
    items: list[ContextPackItemRead] = Field(default_factory=list)


class ContextPackListResponse(PaginatedData[ContextPackRead]):
    pass


class CompileResponse(ApiSchema):
    context_pack: ContextPackDetailRead
    total_token_count: int = Field(ge=0)
    included_count: int = Field(ge=0)
    excluded_count: int = Field(ge=0)
    degradation_reason: str | None = None
