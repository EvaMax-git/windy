"""P4-06 Schemas for ``memory_sources`` table.

Sources link memories to their evidence: candidates, raw_events, assets,
documents, blocks, or messages.  Each source references a specific
``memory_version``.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import Field

from mneme.schemas.common import ApiSchema


class SourceRole(str, Enum):
    """``memory_sources.source_role`` CHECK constraint values."""

    evidence = "evidence"
    origin = "origin"
    supporting = "supporting"
    conflict = "conflict"
    supersedes = "supersedes"


class MemorySourceCreate(ApiSchema):
    """Request body for ``POST /memory/{id}/sources``."""

    memory_version: int = Field(ge=1, description="Which version this source belongs to")
    candidate_id: UUID | None = None
    raw_event_id: UUID | None = None
    asset_id: UUID | None = None
    document_id: UUID | None = None
    block_id: UUID | None = None
    message_id: UUID | None = None
    source_span: dict[str, Any] = Field(default_factory=dict)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    source_role: SourceRole = SourceRole.evidence


class MemorySourceRead(ApiSchema):
    """A single ``memory_sources`` row."""

    memory_source_id: UUID
    memory_id: UUID
    memory_version: int
    candidate_id: UUID | None = None
    raw_event_id: UUID | None = None
    asset_id: UUID | None = None
    document_id: UUID | None = None
    block_id: UUID | None = None
    message_id: UUID | None = None
    source_span: dict[str, Any]
    confidence: float | None = None
    source_role: str
    created_at: datetime | None = None
