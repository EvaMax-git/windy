"""Admin governance API schemas for audit events, events, and event deliveries."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import Field

from mneme.schemas.common import (
    ActorRef,
    ApiSchema,
    PageInfo,
    PaginatedData,
    SensitivityLevel,
)


# ── Audit Events ────────────────────────────────────────────────────────────────


class AdminAuditFilterParams(ApiSchema):
    """Query-string filters for GET /admin/audit-events."""

    actor_type: str | None = None
    action: str | None = None
    result: str | None = None
    object_type: str | None = None
    occurred_after: datetime | None = None
    occurred_before: datetime | None = None


class AdminAuditEventRead(ApiSchema):
    """A single audit_events row for admin display."""

    audit_id: UUID
    occurred_at: datetime
    actor: ActorRef
    action: str
    object_type: str | None = None
    object_id: UUID | None = None
    project_id: UUID | None = None
    result: str
    reason_code: str | None = None
    sensitivity_level: str
    correlation_id: UUID
    request_id: UUID
    review_item_id: UUID | None = None
    diff_summary: dict[str, Any] = Field(default_factory=dict)
    metadata_json: dict[str, Any] = Field(default_factory=dict)


class AdminAuditListResponse(PaginatedData[AdminAuditEventRead]):
    """Paginated list of audit events for admin view."""
    pass


# ── Events (Outbox) ─────────────────────────────────────────────────────────────


class AdminEventFilterParams(ApiSchema):
    """Query-string filters for GET /admin/events."""

    event_type: str | None = None
    publish_state: str | None = None
    aggregate_type: str | None = None
    occurred_after: datetime | None = None
    occurred_before: datetime | None = None


class AdminEventRead(ApiSchema):
    """A single events row for admin display."""

    event_id: UUID
    event_type: str
    aggregate_type: str
    aggregate_id: UUID
    aggregate_version: int
    correlation_id: UUID
    causation_id: UUID | None = None
    idempotency_key: str
    producer: str
    payload_json: dict[str, Any] = Field(default_factory=dict)
    visibility: str
    publish_state: str
    occurred_at: datetime
    committed_at: datetime
    published_at: datetime | None = None
    last_error: str | None = None


class AdminEventDetailResponse(AdminEventRead):
    """Admin event detail including linked deliveries."""

    deliveries: list["AdminDeliveryRead"] = Field(default_factory=list)


class AdminDeliveryRead(ApiSchema):
    """A single event_deliveries row."""

    delivery_id: UUID
    event_id: UUID
    consumer_name: str
    delivery_state: str
    dispatch_attempts: int
    last_dispatched_at: datetime | None = None
    acknowledged_at: datetime | None = None
    failed_at: datetime | None = None
    last_error: str | None = None
    lease_expires_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class AdminEventListResponse(PaginatedData[AdminEventRead]):
    """Paginated list of events for admin view."""
    pass
