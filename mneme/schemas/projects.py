from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import List
from uuid import UUID

from pydantic import Field

from mneme.schemas.common import ApiSchema, PageInfo, SensitivityLevel


class ProjectStatus(str, Enum):
    active = "active"
    archived = "archived"
    disabled = "disabled"


class ProjectRead(ApiSchema):
    project_id: UUID
    project_code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    status: ProjectStatus
    sensitivity_default: SensitivityLevel
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None


class ProjectCreateRequest(ApiSchema):
    project_code: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_-]*$",
        description="Unique project code (lowercase, alphanumeric, dash, underscore).",
    )
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    sensitivity_default: SensitivityLevel = SensitivityLevel.normal


class ProjectCreateResponse(ApiSchema):
    project: ProjectRead


class ProjectUpdateRequest(ApiSchema):
    """Fields that may be updated on an existing project. All are optional."""
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    sensitivity_default: SensitivityLevel | None = None


class ProjectListResponse(ApiSchema):
    items: List[ProjectRead]
    page_info: PageInfo
