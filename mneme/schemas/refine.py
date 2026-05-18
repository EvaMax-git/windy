"""P6 Memory Refine schemas.

Schemas for the refine pipeline: dedup detection, conflict detection,
quality scoring, auto-expiration, and smart merge.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import Field

from mneme.schemas.common import ApiSchema


# ── Pipeline request / response ───────────────────────────────────────────


class RefineRunRequest(ApiSchema):
    """Request body for ``POST /api/v4/memory/refine``."""

    project_id: UUID
    operations: list[str] = Field(
        default=["dedup", "conflict", "quality", "expire"],
        description="Which refine sub-operations to run.",
    )
    dry_run: bool = False
    similarity_threshold: float = Field(default=0.92, ge=0.5, le=1.0)
    max_candidates: int = Field(default=50, ge=1, le=500)


class RefineRunResponse(ApiSchema):
    """Response from a full or partial refine pipeline run."""

    dedup_pairs_found: int = 0
    conflicts_found: int = 0
    merges_executed: int = 0
    expires_executed: int = 0
    quality_scored: int = 0
    details: list[dict[str, Any]] = Field(default_factory=list)


# ── Dedup ─────────────────────────────────────────────────────────────────


class DedupCandidate(ApiSchema):
    """A single duplicate-pair candidate."""

    memory_a_id: UUID
    memory_b_id: UUID
    similarity: float
    memory_a_key: str | None = None
    memory_b_key: str | None = None
    memory_a_title: str | None = None
    memory_b_title: str | None = None


# ── Conflict ──────────────────────────────────────────────────────────────


class ConflictCandidate(ApiSchema):
    """A single conflict-pair candidate (before or after LLM evaluation)."""

    memory_a_id: UUID
    memory_b_id: UUID
    similarity: float
    conflict: bool = False
    reason: str | None = None
    confidence: float = 0.0
    memory_a_key: str | None = None
    memory_b_key: str | None = None


# ── Quality ───────────────────────────────────────────────────────────────


class QualityResult(ApiSchema):
    """Quality scoring result for one memory."""

    memory_id: UUID
    quality_score: float
    search_weight: float
    confidence_component: float = 0.0
    evidence_component: float = 0.0
    coherence_component: float = 0.0
    recency_component: float = 0.0
    relation_component: float = 0.0


# ── Expire ────────────────────────────────────────────────────────────────


class ExpireCandidate(ApiSchema):
    """A memory flagged for auto-expiration."""

    memory_id: UUID
    canonical_key: str | None = None
    reason: str
    quality_score: float | None = None
    search_weight: float | None = None
    created_at: Any = None


# ── Merge ─────────────────────────────────────────────────────────────────


class MergeRequest(ApiSchema):
    """Request body for ``POST /api/v4/memory/refine/merge``."""

    survivor_memory_id: UUID
    consumed_memory_ids: list[UUID] = Field(min_length=1)
    reason: str | None = None
