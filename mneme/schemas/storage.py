"""Pydantic schemas for the P3-01 Storage layer.

These schemas model the inputs and outputs of the file-system storage
backend: staging metadata, promote results, upload outcomes, and
content-hash–dedup responses.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import Field

from mneme.schemas.common import ApiSchema, SensitivityLevel


class InboxType(str, Enum):
    file = "file"
    url = "url"
    text = "text"
    email = "email"
    message = "message"
    api = "api"
    importer = "importer"


class InboxStatus(str, Enum):
    received = "received"
    staged = "staged"
    linked = "linked"
    processed = "processed"
    rejected = "rejected"
    failed = "failed"
    archived = "archived"


class AssetType(str, Enum):
    document = "document"
    image = "image"
    audio = "audio"
    video = "video"
    archive = "archive"
    dataset = "dataset"
    note = "note"
    url = "url"
    other = "other"


class AssetStatus(str, Enum):
    active = "active"
    archived = "archived"
    deleted = "deleted"
    quarantined = "quarantined"


class IngestState(str, Enum):
    pending = "pending"
    staged = "staged"
    importing = "importing"
    ready = "ready"
    failed = "failed"


class KnowledgeState(str, Enum):
    not_started = "not_started"
    pending = "pending"
    running = "running"
    ready = "ready"
    stale = "stale"
    failed = "failed"


class StorageBackend(str, Enum):
    mneme_data = "mneme_data"
    local_path = "local_path"
    external_uri = "external_uri"
    s3_compatible = "s3_compatible"


class RetentionPolicy(str, Enum):
    default = "default"
    short_term = "short_term"
    long_term = "long_term"
    permanent = "permanent"


# ═══════════════════════════════════════════════════════════════════
# Staging
# ═══════════════════════════════════════════════════════════════════


class StagedFileInfo(ApiSchema):
    """Info returned after a file has been staged on disk."""

    staging_path: str = Field(description="Absolute path to the staged file.")
    original_filename: str = Field(description="Sanitized original filename.")
    content_hash: str = Field(description="SHA-256 hex digest of file content.")
    size_bytes: int = Field(description="File size in bytes.", ge=0)
    media_type: str | None = Field(default=None, description="Detected MIME type.")
    staging_token: str = Field(
        description="Opaque token used to reference this staged file in subsequent requests."
    )


class UploadRequest(ApiSchema):
    """Metadata that accompanies a multipart file upload."""

    project_id: UUID = Field(description="Target project for this upload.")
    title: str | None = Field(
        default=None,
        min_length=1,
        max_length=300,
        description="Optional title for the asset. Defaults to the original filename.",
    )
    sensitivity_level: SensitivityLevel = Field(
        default=SensitivityLevel.normal,
        description="Sensitivity level for the asset.",
    )
    inbox_type: InboxType = Field(
        default=InboxType.file,
        description="Type of the inbox item.",
    )


# ═══════════════════════════════════════════════════════════════════
# Content-hash dedup / idempotent upload
# ═══════════════════════════════════════════════════════════════════


class ContentHashDuplicate(ApiSchema):
    """Returned when an uploaded file's content hash matches an existing asset."""

    existing_asset_id: UUID = Field(
        description="ID of the existing asset with the same content hash."
    )
    asset_uid: str = Field(description="Asset UID of the existing asset.")
    title: str = Field(description="Title of the existing asset.")
    content_hash: str = Field(description="SHA-256 content hash that collided.")
    created_at: datetime = Field(description="When the existing asset was created.")


# ═══════════════════════════════════════════════════════════════════
# Inbox item (lightweight read)
# ═══════════════════════════════════════════════════════════════════


class InboxItemRead(ApiSchema):
    inbox_item_id: UUID
    project_id: UUID | None = None
    inbox_type: InboxType
    source: str
    source_uri: str | None = None
    source_ref: str | None = None
    status: InboxStatus
    asset_id: UUID | None = None
    title: str | None = None
    content_hash: str | None = None
    received_at: datetime
    processed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class InboxItemCreateRequest(ApiSchema):
    project_id: UUID
    inbox_type: InboxType
    source: str = "api"
    source_uri: str | None = None
    source_ref: str | None = None
    title: str | None = Field(default=None, max_length=300)
    content_hash: str | None = Field(default=None, max_length=128)
    payload_json: dict | None = Field(default_factory=dict)
    metadata_json: dict | None = Field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════════
# Asset (lightweight read for storage layer consumers)
# ═══════════════════════════════════════════════════════════════════


class AssetRead(ApiSchema):
    asset_id: UUID
    project_id: UUID | None = None
    asset_uid: str
    title: str
    asset_type: AssetType
    media_type: str | None = None
    original_filename: str | None = None
    storage_backend: StorageBackend
    storage_ref: str
    canonical_uri: str | None = None
    content_hash: str
    size_bytes: int | None = None
    status: AssetStatus
    ingest_state: IngestState
    knowledge_state: KnowledgeState
    current_version: int
    sensitivity_level: SensitivityLevel
    retention_policy: RetentionPolicy
    source_inbox_item_id: UUID | None = None
    created_by_user_id: UUID | None = None
    imported_from: str | None = None
    imported_source_id: str | None = None
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None


class AssetCreateRequest(ApiSchema):
    project_id: UUID | None = None
    title: str = Field(min_length=1, max_length=300)
    asset_type: AssetType = AssetType.document
    media_type: str | None = Field(default=None, max_length=120)
    original_filename: str | None = Field(default=None, max_length=255)
    content_hash: str = Field(max_length=128)
    size_bytes: int | None = Field(default=None, ge=0)
    sensitivity_level: SensitivityLevel = SensitivityLevel.normal
    retention_policy: RetentionPolicy = RetentionPolicy.default
    source_inbox_item_id: UUID | None = None
    storage_ref: str = "pending"
    canonical_uri: str | None = None


class AssetUpdateRequest(ApiSchema):
    """Partial update for an asset. Only non-None fields are applied."""

    title: str | None = Field(default=None, min_length=1, max_length=300)
    sensitivity_level: SensitivityLevel | None = None
    retention_policy: RetentionPolicy | None = None


# Re-exported from mneme.schemas.asset_metadata for backward compatibility.
from mneme.schemas.asset_metadata import (  # noqa: F401, E402
    AssetMetadataCreateRequest,
    AssetMetadataRead,
    AssetMetadataUpdateRequest,
    MetadataValueType,
)
