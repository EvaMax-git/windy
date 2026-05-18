"""Schemas for Conversation, EventSource, and Message models (Phase 4).

P4-01 / P4-02 — conversations, event_source, and messages.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import Field

from mneme.schemas.common import ApiSchema, PaginatedData, SensitivityLevel


# ══════════════════════════════════════════════════════════════════════
# Conversation
# ══════════════════════════════════════════════════════════════════════

class ConversationType(str, Enum):
    chat = "chat"
    meeting = "meeting"
    email_thread = "email_thread"
    system_event = "system_event"
    agent_run = "agent_run"


class ConversationStatus(str, Enum):
    active = "active"
    archived = "archived"
    deleted = "deleted"


class ConversationCreateRequest(ApiSchema):
    """Request body for ``POST /api/v4/conversations``."""
    project_id: UUID = Field(description="Required project id.")
    conversation_type: ConversationType = ConversationType.chat
    source_platform: str = Field(
        min_length=1,
        max_length=48,
        description="来源平台标识 (mneme_web, mneme_api, or external platform)",
    )
    title: str | None = Field(default=None, max_length=300)
    sensitivity_level: SensitivityLevel = SensitivityLevel.private
    retention_days: int | None = Field(default=None, ge=1)
    started_at: datetime | None = Field(default=None)

# Backward-compat alias
ConversationCreate = ConversationCreateRequest


class ConversationUpdateRequest(ApiSchema):
    """Request body for ``PATCH /api/v4/conversations/{id}``."""
    title: str | None = Field(default=None, max_length=300)
    sensitivity_level: SensitivityLevel | None = None
    retention_days: int | None = Field(default=None, ge=1)

# Backward-compat alias
ConversationUpdate = ConversationUpdateRequest


class ConversationRead(ApiSchema):
    """Response body for a conversation."""
    conversation_id: UUID
    project_id: UUID | None = None
    owner_user_id: UUID | None = None
    conversation_type: str
    title: str | None = None
    source_platform: str
    sensitivity_level: str
    retention_days: int | None = None
    conversation_status: str
    started_at: datetime | None = None
    ended_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ConversationListResponse(PaginatedData[ConversationRead]):
    """Paginated list of conversations."""
    pass


# ══════════════════════════════════════════════════════════════════════
# Event Source
# ══════════════════════════════════════════════════════════════════════

class EventSourceCreate(ApiSchema):
    """Request body for creating an event source segment."""
    source_platform: str = Field(min_length=1, max_length=48)
    external_conversation_id: str | None = Field(default=None, max_length=255)
    source_account_id: str | None = Field(default=None, max_length=255)
    source_uri: str | None = None
    participants_json: list[dict[str, Any]] = Field(default_factory=list)
    time_range_start: datetime | None = None
    time_range_end: datetime | None = None
    import_run_id: UUID | None = None
    metadata_json: dict[str, Any] = Field(default_factory=dict)


class EventSourceRead(ApiSchema):
    """Response body for an event source segment."""
    event_source_id: UUID
    conversation_id: UUID
    source_platform: str
    external_conversation_id: str | None = None
    source_account_id: str | None = None
    source_uri: str | None = None
    participants_json: list[dict[str, Any]] = Field(default_factory=list)
    time_range_start: datetime | None = None
    time_range_end: datetime | None = None
    import_run_id: UUID | None = None
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


# ══════════════════════════════════════════════════════════════════════
# Message
# ══════════════════════════════════════════════════════════════════════

class RoleCode(str, Enum):
    user = "user"
    assistant = "assistant"
    agent = "agent"
    system = "system"
    tool = "tool"
    other = "other"


class MessageCreate(ApiSchema):
    """Request body for writing a single message."""
    role_code: RoleCode
    content_text: str = Field(min_length=1)
    content_markdown: str | None = None
    message_time: datetime
    event_source_id: UUID | None = None
    parent_message_id: UUID | None = None
    sender_label: str | None = Field(default=None, max_length=120)
    sensitivity_level: SensitivityLevel = SensitivityLevel.private


class MessageBatchCreate(ApiSchema):
    """Request body for batch message import (max 500)."""
    messages: list[MessageCreate] = Field(
        min_length=1,
        max_length=500,
        description="消息数组，最多 500 条",
    )
    event_source_id: UUID | None = Field(
        default=None,
        description="若整个批次共享同一 event_source，可在此指定",
    )


class MessageRead(ApiSchema):
    """Response body for a single message."""
    message_id: UUID
    conversation_id: UUID
    event_source_id: UUID | None = None
    parent_message_id: UUID | None = None
    role_code: str
    sender_label: str | None = None
    content_text: str
    content_markdown: str | None = None
    content_hash: str
    sensitivity_level: str
    pii_flags: list[dict[str, Any]] = Field(default_factory=list)
    message_time: datetime
    ingested_at: datetime
    created_at: datetime
    updated_at: datetime


class BatchImportResult(ApiSchema):
    """Result of a batch message import."""
    imported_count: int
    skipped_duplicates: int
    message_ids: list[UUID]
    first_message_time: datetime | None = None
    last_message_time: datetime | None = None


# ══════════════════════════════════════════════════════════════════════
# Raw Event (P4-03)
# ══════════════════════════════════════════════════════════════════════

class RawEventType(str, Enum):
    message = "message"
    tool_call = "tool_call"
    tool_result = "tool_result"
    agent_thought = "agent_thought"
    system_event = "system_event"
    custom = "custom"


class RawEventCreate(ApiSchema):
    """Request body for ``POST /api/v4/raw-events``."""
    raw_event_type: RawEventType
    source_platform: str = Field(min_length=1, max_length=48)
    payload_json: dict[str, Any] = Field(default_factory=dict)
    event_time: datetime
    event_source_id: UUID | None = None
    conversation_id: UUID | None = None
    message_id: UUID | None = None
    project_id: UUID | None = None
    external_event_id: str | None = Field(default=None, max_length=255)
    sensitivity_level: SensitivityLevel = SensitivityLevel.private
    import_run_id: UUID | None = None
    idempotency_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        description="幂等键；若不提供则自动从 payload_hash + event_time 派生",
    )


class RawEventRead(ApiSchema):
    """Response body for a raw event."""
    raw_event_id: UUID
    project_id: UUID | None = None
    event_source_id: UUID | None = None
    conversation_id: UUID | None = None
    message_id: UUID | None = None
    raw_event_type: str
    source_platform: str
    external_event_id: str | None = None
    event_time: datetime
    payload_hash: str
    payload_json: dict[str, Any] = Field(default_factory=dict)
    text_preview: str | None = None
    sensitivity_level: str
    pii_flags: list[dict[str, Any]] = Field(default_factory=list)
    retention_until: datetime | None = None
    import_run_id: UUID | None = None
    idempotency_key: str
    created_at: datetime

