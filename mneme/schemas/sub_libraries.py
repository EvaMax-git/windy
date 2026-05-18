from __future__ import annotations
from uuid import UUID
from pydantic import Field

from mneme.schemas.common import ApiSchema

from mneme.schemas.common import PageInfo


class SubLibraryCreateRequest(ApiSchema):
    name: str = ""
    type: str = "vector"
    key: str = ""
    capability_json: dict = Field(default_factory=dict)
    metadata_json: dict = Field(default_factory=dict)


class SubLibraryUpdateRequest(ApiSchema):
    name: str | None = None
    type: str | None = None
    key: str | None = None
    capability_json: dict | None = None
    metadata_json: dict | None = None


class SubLibraryRead(ApiSchema):
    id: UUID = Field(default_factory=lambda: UUID(int=0))
    name: str = ""
    type: str = "vector"
    key: str = ""
    capability_json: dict = Field(default_factory=dict)
    metadata_json: dict = Field(default_factory=dict)
    created_at: str | None = None


class SubLibraryListResponse(ApiSchema):
    items: list[SubLibraryRead]
    page_info: PageInfo


SubLibraryType = str  # "vector" | "graph" | "fulltext" | "custom"
