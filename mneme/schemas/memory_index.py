"""P4-07 Memory index schemas for ``memory_index_entries`` table.

Schema alignment
----------------
All enumerations match the DDL CHECK constraints defined in
``0001_baseline_45_tables.py``:

* ``fts_state`` — pending, ready, stale, failed
* ``vector_state`` — pending, ready, stale, failed
* ``embedding_model_id`` — Phase 4 NULL (pgvector deferred to Phase 5+)
* ``embedding vector(1536)`` — Phase 4 NULL, vector_state stays 'pending'
* ``fts_vector tsvector`` — GENERATED ALWAYS AS ... STORED (auto from index_text)
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import Field

from mneme.schemas.common import ApiSchema, PaginatedData


class FtsState(str, Enum):
    """``memory_index_entries.fts_state`` CHECK constraint values."""
    pending = "pending"
    ready = "ready"
    stale = "stale"
    failed = "failed"


class VectorState(str, Enum):
    """``memory_index_entries.vector_state`` CHECK constraint values."""
    pending = "pending"
    ready = "ready"
    stale = "stale"
    failed = "failed"


class MemoryIndexEntryRead(ApiSchema):
    """A single ``memory_index_entries`` row returned by the API.

    ``fts_vector`` is GENERATED ALWAYS STORED — not included (auto-derived).
    ``embedding`` is always NULL in Phase 4 — not included (populated Phase 5+).
    """

    memory_index_entry_id: UUID
    memory_id: UUID
    memory_version: int
    project_id: UUID | None = None
    index_profile: str = "default"
    embedding_model_id: UUID | None = None
    content_hash: str
    index_text: str
    fts_state: str
    vector_state: str
    ready_at: datetime | None = None
    stale_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class MemoryIndexEntryCreate(ApiSchema):
    """Input for creating a ``memory_index_entries`` row (used by index_manager).

    Not exposed as a public API — consumed internally by the DAL layer.
    """

    memory_id: UUID
    memory_version: int
    project_id: UUID
    index_text: str = Field(min_length=1)
    content_hash: str
    fts_state: FtsState = FtsState.ready
    index_profile: str = "default"
    embedding_model_id: UUID | None = None


class MemoryIndexFilterParams(ApiSchema):
    """Query-string filters for ``GET /api/v4/memory/index/states``."""

    project_id: UUID | None = None
    fts_state: FtsState | None = None
    vector_state: VectorState | None = None
    memory_id: UUID | None = None


class MemoryIndexRebuildRequest(ApiSchema):
    """Request body for ``POST /api/v4/memory/index/rebuild-fts``."""

    memory_index_entry_id: UUID | None = None
    memory_id: UUID | None = None
    index_text: str | None = Field(default=None, min_length=1)


class MemoryIndexStateSummary(ApiSchema):
    """Aggregated index state counts."""

    total_entries: int = 0
    fts_ready: int = 0
    fts_stale: int = 0
    fts_pending: int = 0
    fts_failed: int = 0
    vector_ready: int = 0
    vector_pending: int = 0
    vector_stale: int = 0
    vector_failed: int = 0


class MemoryIndexEntryListResponse(PaginatedData[MemoryIndexEntryRead]):
    """Paginated list of index entries."""
    pass


# ── P4-10 Search schemas ────────────────────────────────────────────────────


class MemorySearchParams(ApiSchema):
    """Query parameters for ``GET /api/v4/memory/search``."""
    q: str = Field(min_length=1)
    project_id: UUID | None = None
    store_id: UUID | None = None
    mode: Literal["fts", "vector", "hybrid"] = "hybrid"
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)


class MemorySearchResultItem(ApiSchema):
    """A single search result from the memory FTS index."""
    memory_index_entry_id: UUID
    memory_id: UUID
    memory_version: int
    index_text: str
    fts_state: str
    vector_state: str
    rank: float = 0.0
    fts_rank: float = 0.0
    vector_rank: float = 0.0
    search_mode: str = "fts"
    degraded: bool = False
    degradation_reason: str | None = None
    stale: bool = False
    stale_reason: str | None = None
    title: str | None = None
    memory_text: str
    sensitivity_level: str
    canonical_key: str
    status: str
    current_version: int
    quality_score: float | None = None
    search_weight: float = 1.0


class MemorySearchResponse(PaginatedData[MemorySearchResultItem]):
    """Paginated search results."""
    search_mode: str = "fts"
    degraded: bool = False
    degradation_reason: str | None = None
    stale_count: int = 0
    rerank_applied: bool = False
