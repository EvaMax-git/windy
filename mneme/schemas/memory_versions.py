"""P4-06 Schemas for ``memory_versions`` table.

Version records are created automatically by P4-05 write operations.
These schemas support read-only version history queries.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from mneme.schemas.common import ApiSchema, PaginatedData


class VersionAction(str, Enum):
    """``memory_versions.action`` CHECK constraint values."""

    create = "create"
    update = "update"
    merge = "merge"
    expire = "expire"
    delete = "delete"
    restore = "restore"


class MemoryVersionRead(ApiSchema):
    """A single ``memory_versions`` row."""

    memory_version_id: UUID
    memory_id: UUID
    version: int
    action: str
    before_json: dict[str, Any]
    after_json: dict[str, Any]
    actor_type: str
    actor_id: UUID | None = None
    review_item_id: UUID | None = None
    candidate_id: UUID | None = None
    event_id: UUID | None = None
    reason: str | None = None
    created_at: datetime | None = None


class MemoryVersionFilterParams(ApiSchema):
    """Query-string filters for ``GET /memory/{id}/versions``."""

    action: VersionAction | None = None


class MemoryVersionListResponse(PaginatedData[MemoryVersionRead]):
    """Paginated list of memory versions."""

    pass
