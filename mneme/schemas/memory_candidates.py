"""P4-04 Memory Candidate schemas for ``memory_candidates`` table.

Schema alignment
----------------
All enumerations match the DDL CHECK constraints defined in
``0001_baseline_45_tables.py``:

* ``source_type`` — message, raw_event, manual, importer, agent_submission
* ``candidate_status`` — pending_review, approved, rejected, superseded, conflict
* ``submitted_by_actor_type`` — user, agent, service, system
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import Field

from mneme.schemas.common import ApiSchema, PageInfo, PaginatedData


class CandidateSourceType(str, Enum):
    """``memory_candidates.source_type`` CHECK constraint values."""
    message = "message"
    raw_event = "raw_event"
    manual = "manual"
    importer = "importer"
    agent_submission = "agent_submission"


class CandidateStatus(str, Enum):
    """``memory_candidates.candidate_status`` CHECK constraint values."""
    pending_review = "pending_review"
    approved = "approved"
    rejected = "rejected"
    superseded = "superseded"
    conflict = "conflict"


class CandidateFilterParams(ApiSchema):
    """Query-string filters for ``GET /memory/candidates``."""
    project_id: UUID | None = None
    source_type: CandidateSourceType | None = None
    candidate_status: CandidateStatus | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None


class MemoryCandidateCreate(ApiSchema):
    """Request body for ``POST /memory/candidates``."""
    project_id: UUID | None = None
    source_type: CandidateSourceType
    source_id: UUID | None = None
    title: str | None = Field(default=None, max_length=240)
    candidate_text: str = Field(min_length=1)
    sensitivity_level: str = "private"
    confidence_score: float | None = Field(default=None, ge=0.0, le=1.0)
    review_required: bool = True
    metadata_json: dict[str, Any] = Field(default_factory=dict)


class MemoryCandidateUpdate(ApiSchema):
    """Request body for ``PATCH /memory/candidates/{candidate_id}``."""
    title: str | None = Field(default=None, max_length=240)
    candidate_text: str | None = Field(default=None, min_length=1)
    sensitivity_level: str | None = None
    confidence_score: float | None = Field(default=None, ge=0.0, le=1.0)
    metadata_json: dict[str, Any] | None = None


class MemoryCandidateStatusUpdate(ApiSchema):
    """Request body for approve/reject actions."""
    reason: str | None = None


class MemoryCandidateRead(ApiSchema):
    """A single ``memory_candidates`` row returned by the API."""
    candidate_id: UUID
    project_id: UUID | None = None
    source_type: str
    source_id: UUID | None = None
    submitted_by_actor_type: str
    submitted_by_actor_id: UUID | None = None
    title: str | None = None
    candidate_text: str
    candidate_hash: str
    sensitivity_level: str
    candidate_status: str
    confidence_score: float | None = None
    review_required: bool
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class MemoryCandidateListResponse(PaginatedData[MemoryCandidateRead]):
    """Paginated list of memory candidates."""
    pass


class MemoryExtractRequest(ApiSchema):
    """Request body for ``POST /memory/extract`` — manual trigger of extract pipeline."""
    source_type: str = Field(
        default="message",
        pattern="^(message|raw_event)$",
    )
    source_id: UUID
    project_id: UUID | None = None
    conversation_context: str | None = None


class MemoryExtractResponse(ApiSchema):
    """Response from ``POST /memory/extract``."""
    pipeline_run_id: UUID | None = None
    candidates_submitted: int = 0
    candidates_deduped: int = 0
    sources_created: int = 0
    llm_candidates_found: int = 0
    error: str | None = None
    candidates: list[dict[str, object]] = Field(default_factory=list)
