from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import Field

from mneme.schemas.common import ActorType, ApiSchema, SensitivityLevel


class ObjectType(str, Enum):
    asset = "asset"
    document = "document"
    block = "block"
    knowledge_document = "knowledge_document"
    knowledge_block = "knowledge_block"
    chunk = "chunk"
    conversation = "conversation"
    message = "message"
    raw_event = "raw_event"
    memory_candidate = "memory_candidate"
    memory = "memory"
    context_pack = "context_pack"
    job = "job"
    pipeline_def = "pipeline_def"
    pipeline_run = "pipeline_run"
    project = "project"
    provider_model = "provider_model"
    credential = "credential"
    review_item = "review_item"
    inbox_item = "inbox_item"
    import_run = "import_run"
    backup = "backup"
    restore = "restore"
    external = "external"


class ObjectStatus(str, Enum):
    active = "active"
    archived = "archived"
    deleted = "deleted"
    quarantined = "quarantined"
    superseded = "superseded"


class ObjectVersionAction(str, Enum):
    create = "create"
    update = "update"
    merge = "merge"
    expire = "expire"
    archive = "archive"
    delete = "delete"
    restore = "restore"
    supersede = "supersede"
    import_ = "import"


class ObjectRegistryRead(ApiSchema):
    object_id: UUID
    project_id: UUID | None = None
    object_type: ObjectType
    object_key: str | None = Field(default=None, max_length=255)
    owner_actor_type: ActorType
    owner_actor_id: UUID | None = None
    status: ObjectStatus
    current_version: int = Field(ge=1)
    sensitivity_level: SensitivityLevel
    source_type: str | None = Field(default=None, max_length=80)
    source_id: UUID | None = None
    canonical_uri: str | None = None
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None


class ObjectRegistryCreate(ApiSchema):
    object_id: UUID
    project_id: UUID | None = None
    object_type: ObjectType
    object_key: str | None = Field(default=None, max_length=255)
    owner_actor_type: ActorType = ActorType.system
    owner_actor_id: UUID | None = None
    sensitivity_level: SensitivityLevel = SensitivityLevel.normal
    source_type: str | None = Field(default=None, max_length=80)
    source_id: UUID | None = None
    canonical_uri: str | None = None
    metadata_json: dict[str, Any] = Field(default_factory=dict)


class ObjectVersionRead(ApiSchema):
    object_version_id: UUID
    object_id: UUID
    object_type: ObjectType
    version: int = Field(ge=1)
    action: ObjectVersionAction
    actor_type: ActorType
    actor_id: UUID | None = None
    event_id: UUID | None = None
    audit_id: UUID | None = None
    source_map_id: UUID | None = None
    previous_version: int | None = Field(default=None, ge=1)
    checksum: str | None = Field(default=None, max_length=128)
    snapshot_json: dict[str, Any] = Field(default_factory=dict)
    diff_json: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = None
    created_at: datetime


class ObjectVersionCreate(ApiSchema):
    object_id: UUID
    object_type: ObjectType
    version: int = Field(ge=1)
    action: ObjectVersionAction
    actor_type: ActorType
    actor_id: UUID | None = None
    event_id: UUID | None = None
    audit_id: UUID | None = None
    source_map_id: UUID | None = None
    previous_version: int | None = Field(default=None, ge=1)
    checksum: str | None = Field(default=None, max_length=128)
    snapshot_json: dict[str, Any] = Field(default_factory=dict)
    diff_json: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = None

