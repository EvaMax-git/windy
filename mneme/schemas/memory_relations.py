"""P4-08 Memory Relation schemas for ``memory_relations`` table.

Relation types
--------------
* ``conflicts_with`` — two memories contradict each other.
* ``supersedes`` — one memory replaces an older one.
* ``merged_into`` — old memory absorbed into survivor (P4-05 merge auto-creates).
* ``duplicates`` — highly similar / duplicate content.
* ``supports`` — one memory reinforces / corroborates another.
* ``similar`` — high embedding similarity (cosine >= 0.92).
* ``causal`` — one memory causes / leads to another.
* ``temporal`` — memories close in time, sequential.
* ``contradicts`` — semantic contradiction (conflict zone, 0.70 <= sim < 0.92).
* ``references`` — one memory explicitly references another.

State machine
-------------
::

    active → resolved
    active → cancelled

Constraints
-----------
* ``UNIQUE(from_memory_id, to_memory_id, relation_type)`` — no duplicate for same direction+type.
* ``CHECK(from_memory_id <> to_memory_id)`` — self-reference forbidden.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import Field

from mneme.schemas.common import ApiSchema, PaginatedData


class RelationType(str, Enum):
    """``memory_relations.relation_type`` values."""

    conflicts_with = "conflicts_with"
    supersedes = "supersedes"
    merged_into = "merged_into"
    duplicates = "duplicates"
    supports = "supports"
    similar = "similar"
    causal = "causal"
    temporal = "temporal"
    contradicts = "contradicts"
    references = "references"


class RelationStatus(str, Enum):
    """``memory_relations.relation_status`` values."""

    active = "active"
    resolved = "resolved"
    cancelled = "cancelled"


class MemoryRelationCreate(ApiSchema):
    """Request body for ``POST /api/v4/memory/relations``."""

    from_memory_id: UUID
    to_memory_id: UUID
    relation_type: RelationType
    reason: str | None = None
    created_by_review_item_id: UUID | None = None
    metadata_json: dict[str, Any] = Field(default_factory=dict)


class MemoryRelationUpdate(ApiSchema):
    """Request body for ``PATCH /api/v4/memory/relations/{id}``."""

    reason: str | None = None
    metadata_json: dict[str, Any] | None = None


class MemoryRelationRead(ApiSchema):
    """A single ``memory_relations`` row returned by the API."""

    memory_relation_id: UUID
    project_id: UUID | None = None
    from_memory_id: UUID
    from_memory_version: int | None = None
    to_memory_id: UUID
    to_memory_version: int | None = None
    relation_type: str
    relation_status: str
    created_by_review_item_id: UUID | None = None
    reason: str | None = None
    metadata_json: Any = Field(default_factory=dict)
    created_at: datetime | None = None


class MemoryRelationListResponse(PaginatedData[MemoryRelationRead]):
    """Paginated list of memory relations."""

    pass
