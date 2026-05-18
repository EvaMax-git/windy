"""P4-05 Memory schemas for ``memories`` table.

Schema alignment
----------------
All enumerations match the DDL CHECK constraints defined in
``0001_baseline_45_tables.py``:

* ``status`` — draft, active, expired, merged, deleted
* State machine: draft→active|deleted, active→expired|merged|deleted,
  expired→active|deleted, deleted→active
* Hard constraint: ``status='active'`` ⇒ ``activated_by_review_item_id IS NOT NULL``
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import Field

from mneme.schemas.common import ApiSchema, PaginatedData


class MemoryStatus(str, Enum):
    """``memories.status`` CHECK constraint values."""

    draft = "draft"
    active = "active"
    expired = "expired"
    merged = "merged"
    deleted = "deleted"


class NodeType(str, Enum):
    """Graph node type for memories — classifies the cognitive nature of a memory."""

    episode = "episode"      # "I did X with Y at time T" — event/session memory
    fact = "fact"            # "X is true about Y" — declarative knowledge
    reflection = "reflection" # "I learned/realized X" — meta-cognitive insight
    concept = "concept"       # "X is a Y" — abstract category/definition


class EmotionCharge(str, Enum):
    """``memories.emotion_charge`` CHECK constraint values.

    Inferred from behavioral signals (memory_text content, reinforcement
    patterns) — NEVER manually annotated.
    """

    neutral = "neutral"
    embarrassed = "embarrassed"
    proud = "proud"
    fearful = "fearful"


class MemoryCreate(ApiSchema):
    """Request body for ``POST /api/v4/memory`` — manual creation (status='draft')."""

    project_id: UUID
    store_id: UUID | None = None
    title: str | None = Field(default=None, max_length=240)
    memory_text: str = Field(min_length=1)
    sensitivity_level: str = "private"
    canonical_key: str | None = Field(
        default=None,
        max_length=160,
        description="Optional custom canonical_key. Auto-generated if omitted.",
    )
    node_type: NodeType | None = Field(
        default=None,
        description="Graph node type: episode, fact, reflection, or concept.",
    )


class MemoryUpdate(ApiSchema):
    """Request body for ``PATCH /api/v4/memory/{id}``."""

    title: str | None = Field(default=None, max_length=240)
    memory_text: str | None = Field(default=None, min_length=1)
    sensitivity_level: str | None = None
    store_id: UUID | None = None
    node_type: NodeType | None = None


class MemoryMerge(ApiSchema):
    """Request body for ``POST /api/v4/memory/{id}/merge``."""

    target_memory_id: UUID = Field(
        description="Memory to merge into (surviving memory)"
    )
    reason: str | None = None


class MemoryActivate(ApiSchema):
    """Request body for ``POST /api/v4/memory/activate`` — activate from candidate."""

    candidate_id: UUID
    project_id: UUID
    title: str | None = Field(default=None, max_length=240)
    memory_text: str = Field(min_length=1)
    sensitivity_level: str = "private"
    review_item_id: UUID
    node_type: NodeType | None = Field(
        default=None,
        description="Graph node type: episode, fact, reflection, or concept.",
    )


class MemoryStatusUpdate(ApiSchema):
    """Request body for expire/restore actions."""

    reason: str | None = None


class MemoryRead(ApiSchema):
    """A single ``memories`` row returned by the API."""

    memory_id: UUID
    project_id: UUID | None = None
    store_id: UUID | None = None
    canonical_key: str
    title: str | None = None
    memory_text: str
    current_version: int
    sensitivity_level: str
    status: str
    node_type: str | None = None
    activated_from_candidate_id: UUID | None = None
    activated_by_review_item_id: UUID | None = None
    activated_at: datetime | None = None
    expired_at: datetime | None = None
    quality_score: float | None = None
    search_weight: float = 1.0
    last_refined_at: datetime | None = None
    decay_score: float | None = None
    decay_state: str | None = None
    last_decayed_at: datetime | None = None
    last_reinforced_at: datetime | None = None
    emotion_charge: str = "neutral"
    uncertainty_score: float = 0.5
    last_emotion_inferred_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class MemoryFilterParams(ApiSchema):
    """Query-string filters for ``GET /api/v4/memory``."""

    project_id: UUID | None = None
    store_id: UUID | None = None
    status: MemoryStatus | None = None
    sensitivity_level: str | None = None
    search: str | None = None
    node_type: NodeType | None = None


class MemoryListResponse(PaginatedData[MemoryRead]):
    """Paginated list of memories."""

    pass


class MemoryBatchApproveRequest(ApiSchema):
    """Request body for ``POST /api/v4/memory/approve`` — batch approve drafts."""
    memory_ids: list[UUID] = Field(min_length=1, max_length=200)
    review_item_id: UUID | None = None
    reason: str | None = None


class MemoryBatchRejectRequest(ApiSchema):
    """Request body for ``POST /api/v4/memory/reject`` — batch reject drafts."""
    memory_ids: list[UUID] = Field(min_length=1, max_length=200)
    reason: str | None = None


# ═══════════════════════════════════════════════════════════════════════════
# P4-11 / P4-12: Decay & Emotion API schemas
# ═══════════════════════════════════════════════════════════════════════════


class DecayStateTransition(ApiSchema):
    """A single memory's decay_state transition from one state to another."""

    memory_id: UUID
    canonical_key: str
    from_state: str
    to_state: str
    decay_score: float


class DecayStatusResponse(ApiSchema):
    """Decay state summary for a project (or globally)."""

    project_id: UUID | None = None
    total_active: int = 0
    total_decaying: int = 0
    total_silent: int = 0
    total_archived: int = 0
    avg_decay_score: float = 1.0


class EmotionStatusResponse(ApiSchema):
    """Emotion distribution summary for a project (or globally)."""

    project_id: UUID | None = None
    total_active: int = 0
    total_neutral: int = 0
    total_proud: int = 0
    total_embarrassed: int = 0
    total_fearful: int = 0
    avg_uncertainty: float = 0.0


class EmotionInferRequest(ApiSchema):
    """Request body for ``POST /api/v4/memory/emotion-infer``."""

    limit: int | None = Field(default=None, ge=1, le=1000,
                               description="Max memories to process (defaults to config setting).")


class ReinforceRequest(ApiSchema):
    """Request body for ``POST /api/v4/memory/{memory_id}/reinforce``."""

    bonus: float | None = Field(default=None, ge=0.0, le=1.0,
                                 description="Reinforcement bonus (defaults to config setting).")
    reason: str | None = Field(default=None, description="Why this reinforcement happened.")


class BatchOperationSummary(ApiSchema):
    """Item-level result within a batch operation."""
    memory_id: UUID
    status: str  # "succeeded" | "failed"
    new_status: str | None = None
    canonical_key: str | None = None
    error: str | None = None


class BatchOperationResult(ApiSchema):
    """Aggregated result from a batch memory operation."""
    total: int
    succeeded: int
    failed: int
    results: list[BatchOperationSummary]
