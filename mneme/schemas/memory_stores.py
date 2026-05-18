from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import Field

from mneme.schemas.common import ApiSchema


class MemoryStoreType(str, Enum):
    memory_card = "memory_card"
    identity = "identity"
    skill = "skill"
    rule = "rule"
    tool = "tool"


class MemoryStoreCreateRequest(ApiSchema):
    name: str = Field(min_length=1, max_length=200)
    type: MemoryStoreType
    agent_id: UUID | None = None
    description: str | None = Field(default=None, max_length=2000)


class MemoryStoreUpdateRequest(ApiSchema):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    type: MemoryStoreType | None = None
    agent_id: UUID | None = None
    description: str | None = Field(default=None, max_length=2000)


class MemoryStoreRead(ApiSchema):
    store_id: UUID
    agent_id: UUID | None = None
    name: str
    type: MemoryStoreType
    description: str | None = None
    created_at: datetime
    updated_at: datetime
