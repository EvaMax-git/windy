"""P2-05 Review Items API schemas for ``review_items`` table.

Schema alignment
----------------
All enumerations and field names match the DDL CHECK constraints defined in
``0001_baseline_45_tables.py``:

* ``review_type`` – 7 values (memory_candidate, sensitive_access, …)
* ``status`` – 6 values (pending → in_review → approved / rejected / cancelled / expired)
* ``target_type`` – 9 values (memory_candidate, memory, asset, job, dead_letter, …)
* ``decision`` – 4 values (approved, rejected, cancelled, expired; nullable)
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import Field

from mneme.schemas.common import ApiSchema, PageInfo, PaginatedData, PaginationParams


# ── Shared enums (align with DDL CHECK constraints) ───────────────────────────


class ReviewType(str, Enum):
    """``review_items.review_type`` CHECK constraint values."""

    memory_candidate = "memory_candidate"
    sensitive_access = "sensitive_access"
    high_cost_call = "high_cost_call"
    import_confirm = "import_confirm"
    restore_confirm = "restore_confirm"
    dlq_replay = "dlq_replay"
    manual = "manual"


class ReviewStatus(str, Enum):
    """``review_items.status`` CHECK constraint values.

    State machine::

        pending → in_review → approved / rejected
        pending → cancelled
        pending → expired
    """

    pending = "pending"
    in_review = "in_review"
    approved = "approved"
    rejected = "rejected"
    cancelled = "cancelled"
    expired = "expired"


class ReviewTargetType(str, Enum):
    """``review_items.target_type`` CHECK constraint values."""

    memory_candidate = "memory_candidate"
    memory = "memory"
    asset = "asset"
    job = "job"
    dead_letter = "dead_letter"
    provider_call = "provider_call"
    credential = "credential"
    import_run = "import_run"
    restore_run = "restore_run"


class ReviewDecision(str, Enum):
    """``review_items.decision`` CHECK constraint values."""

    approved = "approved"
    rejected = "rejected"
    cancelled = "cancelled"
    expired = "expired"


class RequesterActorType(str, Enum):
    """``review_items.requester_actor_type`` CHECK constraint values."""

    user = "user"
    agent = "agent"
    service = "service"
    system = "system"


# ── Filter / pagination parameters ────────────────────────────────────────────


class ReviewItemFilterParams(ApiSchema):
    """Query-string filters for ``GET /review/items``."""

    review_type: ReviewType | None = None
    status: ReviewStatus | None = None
    target_type: ReviewTargetType | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None


# ── Create model ──────────────────────────────────────────────────────────────


class ReviewItemCreate(ApiSchema):
    """Request body for ``POST /review/items``."""

    project_id: UUID | None = None
    review_type: ReviewType
    target_type: ReviewTargetType
    target_id: UUID
    priority: int = Field(default=100, ge=0, le=1000)
    due_at: datetime | None = None
    expires_at: datetime | None = None
    decision_payload: dict[str, Any] = Field(default_factory=dict)


# ── Read model ────────────────────────────────────────────────────────────────


class ReviewItemRead(ApiSchema):
    """A single ``review_items`` row returned by the API."""

    review_item_id: UUID
    project_id: UUID | None = None
    review_type: str
    target_type: str
    target_id: UUID
    target_version: int | None = None
    status: str
    priority: int
    requester_actor_type: str
    requester_actor_id: UUID | None = None
    reviewer_id: UUID | None = None
    decision: str | None = None
    reason: str | None = None
    decision_payload: dict[str, Any] = Field(default_factory=dict)
    due_at: datetime | None = None
    decided_at: datetime | None = None
    expires_at: datetime | None = None
    correlation_id: UUID
    request_id: UUID
    idempotency_key: str
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ── Approve / Reject request bodies ───────────────────────────────────────────


class ReviewItemApproveRequest(ApiSchema):
    """Request body for ``POST /review/items/{id}/approve``."""

    reason: str | None = None


class ReviewItemRejectRequest(ApiSchema):
    """Request body for ``POST /review/items/{id}/reject``."""

    reason: str | None = None


# ── List response ─────────────────────────────────────────────────────────────


class ReviewItemListResponse(PaginatedData[ReviewItemRead]):
    """Paginated list of review items."""
    pass


class ReviewItemBatchRequest(ApiSchema):
    """Request body for batch review-item operations (claim/approve/reject/cancel)."""
    review_item_ids: list[UUID] = Field(min_length=1, max_length=200)
    reason: str | None = None


class ReviewItemBatchSummary(ApiSchema):
    """Item-level result within a batch review operation."""
    review_item_id: UUID
    status: str  # "succeeded" | "skipped" | "failed"
    new_status: str | None = None
    error: str | None = None


class ReviewItemBatchResult(ApiSchema):
    """Aggregated result from a batch review-item operation."""
    total: int
    succeeded: int
    skipped: int
    failed: int
    results: list[ReviewItemBatchSummary]
