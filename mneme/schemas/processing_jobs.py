from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field

from mneme.schemas.common import ApiSchema


class ProcessingJobCreateRequest(ApiSchema):
    """Request to create a processing job from an existing asset."""
    asset_id: UUID = Field(..., description="Asset to process")
    pipeline_id: UUID = Field(..., description="Pipeline registry ID to use")
    target_stores: list[str] = Field(default_factory=list, description="Target sub-library IDs or keys")


class ProcessingJobRead(ApiSchema):
    """Full processing job record."""
    id: UUID
    asset_id: UUID
    pipeline_id: UUID
    target_stores: list[str] = Field(default_factory=list)
    status: str = "queued"
    chunks_produced: int = 0
    error: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime | None = None


class ProcessingJobStatus(ApiSchema):
    """Lightweight status response for polling."""
    job_id: UUID
    asset_id: UUID
    status: str
    chunks_produced: int = 0
    error: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime | None = None
