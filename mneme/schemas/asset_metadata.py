"""Pydantic schemas for Asset Metadata domain — Create / Read / Update.

Asset metadata provides a flexible key-value store attached to each asset.
Each entry is uniquely identified by ``(asset_id, metadata_key, source)``
and carries a declared ``value_type`` for validation.

Typical sources include:

* ``"manual"`` — user-provided metadata via the API.
* ``"system"`` — automatically extracted (e.g. from file headers).
* ``"ai"`` — AI-inferred metadata (e.g. content classification).

Value-type validation (mirroring the ``asset_metadata.value_type`` CHECK constraint):

* ``text``: any string (including empty).
* ``number``: a valid numeric string.
* ``boolean``: ``true/false/1/0/yes/no`` (case-insensitive).
* ``date``: ISO 8601 ``YYYY-MM-DD``.
* ``json``: valid JSON.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import Field

from mneme.schemas.common import ApiSchema


# ═══════════════════════════════════════════════════════════════════
# Enums
# ═══════════════════════════════════════════════════════════════════


class MetadataValueType(str, Enum):
    """Valid value types for asset_metadata entries.

    Mirrors the CHECK constraint on ``asset_metadata.value_type``:
    ``CHECK (value_type IN ('text', 'number', 'boolean', 'date', 'json'))``
    """

    text = "text"
    number = "number"
    boolean = "boolean"
    date = "date"
    json = "json"


class MetadataSource(str, Enum):
    """Namespaced origin of a metadata entry.

    Helps disambiguate entries with the same ``metadata_key`` that were
    produced by different subsystems.
    """

    manual = "manual"
    system = "system"
    ai = "ai"
    importer = "importer"
    pipeline = "pipeline"


# ═══════════════════════════════════════════════════════════════════
# Read schema
# ═══════════════════════════════════════════════════════════════════


class AssetMetadataRead(ApiSchema):
    """Full representation of an asset metadata entry returned by read / list endpoints."""

    asset_metadata_id: UUID = Field(description="Primary key of the metadata row.")
    asset_id: UUID = Field(description="The asset this metadata belongs to.")
    metadata_key: str = Field(description="Key name (max 120 chars).")
    metadata_value: str | None = Field(
        default=None, description="String representation of the value."
    )
    value_type: str = Field(
        description="Declared type: 'text', 'number', 'boolean', 'date', or 'json'."
    )
    source: str = Field(
        description="Origin namespace: 'manual', 'system', 'ai', etc."
    )
    confidence: float | None = Field(
        default=None, ge=0.0, le=1.0, description="Optional confidence score (0–1)."
    )
    created_at: datetime = Field(description="When this metadata entry was created.")
    updated_at: datetime = Field(description="When this metadata entry was last updated.")


# ═══════════════════════════════════════════════════════════════════
# Create schema
# ═══════════════════════════════════════════════════════════════════


class AssetMetadataCreateRequest(ApiSchema):
    """Payload for adding or upserting a metadata entry on an asset.

    If an entry with the same ``(asset_id, metadata_key, source)`` already
    exists it will be updated (upsert semantics).
    """

    metadata_key: str = Field(
        min_length=1,
        max_length=120,
        description="Metadata key name.",
    )
    metadata_value: str | None = Field(
        default=None,
        description="String value. Must conform to the declared value_type.",
    )
    value_type: MetadataValueType = Field(
        default=MetadataValueType.text,
        description="Declared type for validation.",
    )
    source: str = Field(
        default="manual",
        min_length=1,
        max_length=80,
        description="Origin namespace.",
    )
    confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Optional confidence score (0–1).",
    )
    metadata_json: dict | None = Field(
        default_factory=dict,
        description="Additional structured data stored alongside the value.",
    )


# ═══════════════════════════════════════════════════════════════════
# Update schema
# ═══════════════════════════════════════════════════════════════════


class AssetMetadataUpdateRequest(ApiSchema):
    """Partial update for a metadata entry.  Only non-None fields are applied.

    Notes
    -----
    * If ``value_type`` is changed, the existing ``metadata_value`` is
      re-validated against the new type.
    * ``confidence`` must be in [0.0, 1.0] when provided.
    * ``metadata_json`` fully replaces the existing JSON payload when set.
    """

    metadata_value: str | None = Field(
        default=None,
        description="New string value (or None to keep existing).",
    )
    value_type: MetadataValueType | None = Field(
        default=None,
        description="New value type (or None to keep existing).",
    )
    confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="New confidence score (or None to keep existing).",
    )
    metadata_json: dict | None = Field(
        default=None,
        description="Replacement JSON payload (or None to keep existing).",
    )
