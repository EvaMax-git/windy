"""Pydantic schemas for ``trust_accounts`` table.

信任账户 — per-subject trust ledger tracking call counts, success rate,
and user feedback. Used to compute a composite ``trust_score``.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import Field

from mneme.schemas.common import ApiSchema, PageInfo, PaginatedData, PaginationParams


# ── Enums ─────────────────────────────────────────────────────────────────────


class TrustSubjectType(str, Enum):
    """``trust_accounts.subject_type`` CHECK constraint values."""

    agent = "agent"
    user = "user"
    service = "service"
    system = "system"


# ── Filter / pagination parameters ────────────────────────────────────────────


class TrustAccountFilterParams(ApiSchema):
    """Query-string filters for listing trust_accounts."""

    subject_type: TrustSubjectType | None = None
    subject_id: UUID | None = None
    min_trust_score: float | None = Field(default=None, ge=0.0, le=1.0)
    max_trust_score: float | None = Field(default=None, ge=0.0, le=1.0)


# ── Create model ──────────────────────────────────────────────────────────────


class TrustAccountCreate(ApiSchema):
    """Request body for ``POST /trust/accounts`` to create or get-or-create
    a trust account for a subject.
    """

    subject_type: TrustSubjectType
    subject_id: UUID
    capability_id: UUID | None = None
    metadata_json: dict[str, Any] = Field(default_factory=dict)


# ── Update model ──────────────────────────────────────────────────────────────


class TrustAccountRecordCall(ApiSchema):
    """Request body for ``POST /trust/accounts/{id}/record-call``.

    Records the outcome of a single API / capability call for the subject.
    """

    success: bool = True
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int | None = None
    metadata_json: dict[str, Any] = Field(default_factory=dict)


class TrustAccountRecordFeedback(ApiSchema):
    """Request body for ``POST /trust/accounts/{id}/record-feedback``.

    Records user feedback (positive / negative / neutral) on the subject.
    """

    feedback_type: str = Field(
        description="One of: 'positive', 'negative', 'neutral'",
    )
    comment: str | None = None


# ── Read model ────────────────────────────────────────────────────────────────


class TrustAccountRead(ApiSchema):
    """A single ``trust_accounts`` row returned by the API."""

    trust_account_id: UUID
    subject_type: str
    subject_id: UUID
    capability_id: UUID | None = None
    total_calls: int
    successful_calls: int
    failed_calls: int
    success_rate: float
    positive_feedback: int
    negative_feedback: int
    neutral_feedback: int
    trust_score: float
    last_evaluated_at: datetime | None = None
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ── List response ─────────────────────────────────────────────────────────────


class TrustAccountListResponse(PaginatedData[TrustAccountRead]):
    """Paginated list of trust_accounts."""
    pass
