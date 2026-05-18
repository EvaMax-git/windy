"""Pydantic schemas for ``neg_space_events`` table.

负空间记录 — tracks events where the AI avoids topics, deletes sentences,
or remains silent during conversations.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import Field

from mneme.schemas.common import ApiSchema, PageInfo, PaginatedData, PaginationParams


# ── Enums (align with DDL CHECK constraints) ──────────────────────────────────


class NegSpaceEventCategory(str, Enum):
    """``neg_space_events.event_category`` CHECK constraint values."""

    topic_avoided = "topic_avoided"
    sentence_deleted = "sentence_deleted"
    silence = "silence"
    refusal = "refusal"
    redirection = "redirection"
    other = "other"


class NegSpaceSeverity(str, Enum):
    """``neg_space_events.severity`` CHECK constraint values."""

    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


# ── Filter / pagination parameters ────────────────────────────────────────────


class NegSpaceEventFilterParams(ApiSchema):
    """Query-string filters for listing neg_space_events."""

    event_category: NegSpaceEventCategory | None = None
    severity: NegSpaceSeverity | None = None
    agent_id: UUID | None = None
    conversation_id: UUID | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None


# ── Create model ──────────────────────────────────────────────────────────────


class NegSpaceEventCreate(ApiSchema):
    """Request body for ``POST /neg-space/events``."""

    agent_id: UUID | None = None
    conversation_id: UUID | None = None
    message_id: UUID | None = None
    event_category: NegSpaceEventCategory
    event_type: str = Field(
        min_length=1,
        max_length=64,
        description="Fine-grained event type, e.g. 'topic_avoided.politics'",
    )
    trigger_text: str | None = None
    reason: str | None = None
    severity: NegSpaceSeverity = NegSpaceSeverity.medium
    context_json: dict[str, Any] = Field(default_factory=dict)


# ── Read model ────────────────────────────────────────────────────────────────


class NegSpaceEventRead(ApiSchema):
    """A single ``neg_space_events`` row returned by the API."""

    event_id: UUID
    agent_id: UUID | None = None
    conversation_id: UUID | None = None
    message_id: UUID | None = None
    event_category: str
    event_type: str
    trigger_text: str | None = None
    reason: str | None = None
    severity: str
    context_json: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


# ── List response ─────────────────────────────────────────────────────────────


class NegSpaceEventListResponse(PaginatedData[NegSpaceEventRead]):
    """Paginated list of neg_space_events."""
    pass


# ── Summary / aggregation ─────────────────────────────────────────────────────


class NegSpaceSummary(ApiSchema):
    """Aggregated summary of neg_space_events for a given scope."""

    total_events: int
    by_category: dict[str, int] = Field(default_factory=dict)
    by_severity: dict[str, int] = Field(default_factory=dict)
    latest_event_at: datetime | None = None
