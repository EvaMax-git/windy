"""Admin log schemas — API call log filtering and pagination.

Filters
-------
* ``level`` – call_state (succeeded, failed, timeout, cancelled, denied, dead_letter)
* ``source`` – provider_id or actor_type
* ``time`` – created_at range (since / until)
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field

from mneme.schemas.common import ApiSchema, PaginatedData


# ── Admin Log Filter Params ────────────────────────────────────────────────────


class AdminLogFilterParams(ApiSchema):
    """Query parameters for filtering admin logs."""

    level: str | None = Field(
        default=None,
        description="Filter by call state: succeeded, failed, timeout, cancelled, denied, dead_letter, in_flight",
    )
    source: str | None = Field(
        default=None,
        description="Filter by source: actor_type or provider_id (UUID string)",
    )
    since: datetime | None = Field(
        default=None,
        description="Filter logs created at or after this ISO-8601 timestamp",
    )
    until: datetime | None = Field(
        default=None,
        description="Filter logs created at or before this ISO-8601 timestamp",
    )
    call_type: str | None = Field(
        default=None,
        description="Filter by call_type: chat, embedding, completion, etc.",
    )


# ── Admin Log Item ─────────────────────────────────────────────────────────────


class AdminLogEntry(ApiSchema):
    """A single API call log entry (lightweight admin view)."""

    api_call_log_id: UUID
    request_id: UUID | None = None
    correlation_id: UUID | None = None
    actor_type: str = "system"
    provider_id: UUID | None = None
    call_type: str = "chat"
    call_state: str = "planned"  # level
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    latency_ms: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    retry_count: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime | None = None


class AdminLogListResponse(PaginatedData[AdminLogEntry]):
    """Paginated list of admin log entries."""
    pass
