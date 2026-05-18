from __future__ import annotations
from uuid import UUID
from pydantic import Field

from mneme.schemas.common import ApiSchema


class PipelineRegistryCreateRequest(ApiSchema):
    name: str = ""
    input_formats: list = Field(default_factory=list)
    processor_module: str = ""
    accept_chunk_types: list = Field(default_factory=list)
    target_stores: list = Field(default_factory=list)
    metadata_json: dict = Field(default_factory=dict)


class PipelineRegistryRead(ApiSchema):
    id: UUID = Field(default_factory=lambda: UUID(int=0))
    name: str = ""
    input_formats: list = Field(default_factory=list)
    processor_module: str = ""
    accept_chunk_types: list = Field(default_factory=list)
    target_stores: list = Field(default_factory=list)
    metadata_json: dict = Field(default_factory=dict)
    created_at: str | None = None
