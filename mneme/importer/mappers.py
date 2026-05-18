"""Field mapping definitions — Mneme2 items → v4.1 assets.

Each mapping is a :class:`FieldMappingSchema` that declares how legacy
fields are transformed into v4.1 asset fields.

Mapping strategies
------------------
* **direct_copy**  — value copied verbatim
* **transform**    — value passed through a named transform function
* **computed**     — value derived from multiple fields or context
* **skip**         — field intentionally not mapped

Transform functions are registered in :data:`TRANSFORM_REGISTRY`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from mneme.schemas.importer import (
    FieldMappingEntry,
    FieldMappingSchema,
    ImportSourceType,
)

# ═══════════════════════════════════════════════════════════════════════════════
# Default mapping: mneme2_item → v4.1 Asset
# ═══════════════════════════════════════════════════════════════════════════════

MNEME2_ITEM_MAPPING = FieldMappingSchema(
    source_type=ImportSourceType.mneme2_item,
    version="1.0.0",
    mappings=[
        FieldMappingEntry(
            legacy_field="title",
            target_field="Asset.title",
            strategy="direct_copy",
            required=True,
            notes="Direct copy from Mneme2 item.title",
        ),
        FieldMappingEntry(
            legacy_field="content_type",
            target_field="Asset.media_type",
            strategy="transform",
            transform="normalize_media_type",
            required=False,
            notes="Normalize MIME/extension to v4.1 media_type",
        ),
        FieldMappingEntry(
            legacy_field="content_type",
            target_field="Asset.asset_type",
            strategy="transform",
            transform="derive_asset_type",
            required=False,
            notes="Derive asset_type from content_type",
        ),
        FieldMappingEntry(
            legacy_field="content_hash",
            target_field="Asset.content_hash",
            strategy="direct_copy",
            required=True,
            notes="Content integrity hash",
        ),
        FieldMappingEntry(
            legacy_field="size_bytes",
            target_field="Asset.size_bytes",
            strategy="direct_copy",
            required=False,
        ),
        FieldMappingEntry(
            legacy_field="content_uri",
            target_field="InboxItem.source_uri",
            strategy="direct_copy",
            required=False,
            notes="Original file URI",
        ),
        FieldMappingEntry(
            legacy_field="legacy_id",
            target_field="InboxItem.source_ref",
            strategy="transform",
            transform="format_legacy_ref",
            required=False,
            notes="Store legacy table:row reference",
        ),
        FieldMappingEntry(
            legacy_field="legacy_id",
            target_field="Asset.imported_source_id",
            strategy="direct_copy",
            required=False,
            notes="Provenance tracking",
        ),
        FieldMappingEntry(
            legacy_field="tags",
            target_field="AssetMetadata.metadata_value",
            strategy="transform",
            transform="tags_to_json",
            required=False,
            notes="Store tags as JSON metadata",
        ),
        FieldMappingEntry(
            legacy_field="metadata",
            target_field="Asset.metadata_json",
            strategy="transform",
            transform="merge_metadata",
            required=False,
            notes="Merge legacy metadata into asset.metadata_json",
        ),
        FieldMappingEntry(
            legacy_field="author",
            target_field="AssetMetadata.metadata_value",
            strategy="direct_copy",
            required=False,
            notes="Store author as metadata",
        ),
        FieldMappingEntry(
            legacy_field="created_at",
            target_field="AssetMetadata.metadata_value",
            strategy="direct_copy",
            required=False,
            notes="Original creation time as metadata",
        ),
    ],
)
"""Default field mapping for Mneme2 items → v4.1 assets."""

# ═══════════════════════════════════════════════════════════════════════════════
# Mapping registry
# ═══════════════════════════════════════════════════════════════════════════════

MAPPING_REGISTRY: dict[ImportSourceType, FieldMappingSchema] = {
    ImportSourceType.mneme2_item: MNEME2_ITEM_MAPPING,
}
"""Registry of field mappings by source type."""


def get_mapping(source_type: ImportSourceType) -> FieldMappingSchema | None:
    """Look up a field mapping by source type.

    Args:
        source_type: The source type to look up.

    Returns:
        The mapping schema, or ``None`` if not registered.
    """
    return MAPPING_REGISTRY.get(source_type)


# ═══════════════════════════════════════════════════════════════════════════════
# Transform registry — functions that apply field-level transforms
# ═══════════════════════════════════════════════════════════════════════════════


def _normalize_media_type(value: Any, _item: Any) -> str | None:
    """Normalize a MIME type or file extension to a standard media_type."""
    if not value:
        return None
    s = str(value).lower().strip()
    # Simple extension→MIME mapping
    ext_map = {
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".json": "application/json",
        ".csv": "text/csv",
        ".zip": "application/zip",
    }
    if s.startswith("."):
        return ext_map.get(s, "application/octet-stream")
    return s


def _derive_asset_type(value: Any, _item: Any) -> str:
    """Derive v4.1 asset_type from content_type/MIME."""
    if not value:
        return "other"
    s = str(value).lower()
    if s.startswith("text/") or s.startswith("application/pdf"):
        return "document"
    if s.startswith("image/"):
        return "image"
    if s.startswith("audio/"):
        return "audio"
    if s.startswith("video/"):
        return "video"
    if s in ("application/zip", "application/gzip", "application/x-tar"):
        return "archive"
    return "other"


def _format_legacy_ref(value: Any, _item: Any) -> str:
    """Format legacy_id as 'mneme2:{id}'."""
    return f"mneme2:{value}"


def _tags_to_json(value: Any, _item: Any) -> str | None:
    """Convert tags list to JSON string."""
    import json
    if isinstance(value, list):
        return json.dumps(value)
    if isinstance(value, str):
        return json.dumps([value])
    return None


def _merge_metadata(value: Any, _item: Any) -> dict[str, Any]:
    """Pass through metadata dict."""
    if isinstance(value, dict):
        return value
    return {}


TRANSFORM_REGISTRY: dict[str, Any] = {
    "normalize_media_type": _normalize_media_type,
    "derive_asset_type": _derive_asset_type,
    "format_legacy_ref": _format_legacy_ref,
    "tags_to_json": _tags_to_json,
    "merge_metadata": _merge_metadata,
}
"""Registry of named transform functions."""


def apply_transform(name: str, value: Any, item: Any) -> Any:
    """Apply a named transform to a value.

    Args:
        name: Transform function name.
        value: The field value to transform.
        item: The full source item for context.

    Returns:
        Transformed value.

    Raises:
        ValueError: If the transform is not registered.
    """
    fn = TRANSFORM_REGISTRY.get(name)
    if fn is None:
        raise ValueError(f"Unknown transform: '{name}'")
    return fn(value, item)
