"""P2-03 DLQ API schemas for ``dead_letters`` table queries.

Schema alignment
----------------
All enumerations and field names match the DDL CHECK constraints defined in
``0001_baseline_45_tables.py``:

* ``failure_class`` – 5 values (provider_transient_exhausted, …)
* ``replay_state`` – 5 values (pending → under_review → replayed / cancelled / resolved)
* ``source_type`` – 4 values (event_delivery, job, provider_call, importer)
* ``external_effect_state`` – 4 values (none, unknown, confirmed_done, confirmed_not_done)
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import Field

from mneme.schemas.common import ApiSchema, PageInfo, PaginatedData, PaginationParams


# ── Shared enums (align with DDL CHECK constraints) ───────────────────────────


class FailureClass(str, Enum):
    """``dead_letters.failure_class`` CHECK constraint values."""

    provider_transient_exhausted = "provider_transient_exhausted"
    policy_denied_terminal = "policy_denied_terminal"
    payload_invalid = "payload_invalid"
    code_bug = "code_bug"
    external_side_effect_unknown = "external_side_effect_unknown"


class ReplayState(str, Enum):
    """``dead_letters.replay_state`` CHECK constraint values."""

    pending = "pending"
    under_review = "under_review"
    replayed = "replayed"
    cancelled = "cancelled"
    resolved = "resolved"


class SourceType(str, Enum):
    """``dead_letters.source_type`` CHECK constraint values."""

    event_delivery = "event_delivery"
    job = "job"
    provider_call = "provider_call"
    importer = "importer"


class ExternalEffectState(str, Enum):
    """``dead_letters.external_effect_state`` CHECK constraint values."""

    none = "none"
    unknown = "unknown"
    confirmed_done = "confirmed_done"
    confirmed_not_done = "confirmed_not_done"


# ── Filter / pagination parameters ────────────────────────────────────────────


class DeadLetterFilterParams(ApiSchema):
    """Query-string filters for ``GET /admin/dead-letters``."""

    failure_class: FailureClass | None = None
    replay_state: ReplayState | None = None
    source_type: SourceType | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None


# ── Read model (single row) ───────────────────────────────────────────────────


class DeadLetterRead(ApiSchema):
    """A single ``dead_letters`` row returned by the admin API.

    Every field maps 1:1 to a DDL column so the API consumer has full
    visibility into the dead-letter record.
    """

    dead_letter_id: UUID
    source_type: str
    source_id: UUID
    related_event_id: UUID | None = None
    aggregate_type: str | None = None
    aggregate_id: UUID | None = None
    failure_class: str
    error_code: str | None = None
    error_message: str
    retry_exhausted: bool = False
    external_effect_state: str = "none"
    replay_state: str = "pending"
    review_required: bool = False
    payload_json: dict[str, Any] = Field(default_factory=dict)
    first_failed_at: datetime | None = None
    last_failed_at: datetime | None = None
    replayed_at: datetime | None = None
    resolved_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ── List response ─────────────────────────────────────────────────────────────


class DeadLetterListResponse(PaginatedData[DeadLetterRead]):
    """Paginated list of dead-letter records."""
    pass
