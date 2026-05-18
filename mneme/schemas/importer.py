"""Pydantic schemas for P3-09 Importer skeleton.

The importer provides dry-run / preview / formal-import endpoints that
reuse ``inbox_items`` (inbox_type='importer') and ``pipeline_runs``
(trigger_type='importer', pipeline_type='importer') as the persistence layer.

Design
------
* **ImportRun** — tracks a single import execution (wraps pipeline_runs)
* **FieldMapping** — Mneme2 item → v4.1 asset field correspondence
* **ImportReport** — JSON/Markdown report after dry-run or import
* **ImportSourceItem** — canonical legacy item format (Mneme2 shape)
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import Field

from mneme.schemas.common import ApiSchema, PageInfo, PaginationParams


# ═══════════════════════════════════════════════════════════════════════════════
# Enums
# ═══════════════════════════════════════════════════════════════════════════════


class ImportStatus(str, Enum):
    """Mirror of pipeline_runs.status for importer-scoped runs."""
    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


class ImportRunMode(str, Enum):
    dry_run = "dry_run"
    preview = "preview"
    import_ = "import"


class ImportSourceType(str, Enum):
    """What kind of Mneme2 source data is being imported."""
    mneme2_item = "mneme2_item"
    mneme2_chat = "mneme2_chat"
    mneme2_raw_event = "mneme2_raw_event"
    external_file = "external_file"
    external_json = "external_json"


# ═══════════════════════════════════════════════════════════════════════════════
# Field mapping definition (Mneme2 → v4.1)
# ═══════════════════════════════════════════════════════════════════════════════


class FieldMappingEntry(ApiSchema):
    """A single field mapping: legacy field → v4.1 target field + strategy.

    Example::

        {
            "legacy_field": "item_title",
            "target_field": "Asset.title",
            "strategy": "direct_copy",
            "transform": null,
            "required": true,
            "notes": "Direct copy from Mneme2 item.title"
        }
    """
    legacy_field: str = Field(description="Field name in the Mneme2 source schema.")
    target_field: str = Field(description="Target path in v4.1 (e.g. 'Asset.title').")
    strategy: str = Field(
        default="direct_copy",
        description="Mapping strategy: direct_copy | transform | computed | skip",
    )
    transform: str | None = Field(
        default=None,
        description="Transform function name (e.g. 'hash_content', 'derive_asset_uid').",
    )
    required: bool = Field(default=True, description="Whether this field is required.")
    notes: str | None = Field(default=None, description="Human-readable notes.")


class FieldMappingSchema(ApiSchema):
    """Complete field mapping definition for a source type.

    Contains the ordered list of field mappings plus metadata.
    """

    source_type: ImportSourceType = Field(description="What source type this mapping applies to.")
    version: str = Field(default="1.0.0", description="Mapping schema version.")
    mappings: list[FieldMappingEntry] = Field(
        description="Ordered list of field mappings."
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Import source item (Mneme2 legacy shape)
# ═══════════════════════════════════════════════════════════════════════════════


class ImportSourceItem(ApiSchema):
    """One item in a batch import payload, representing Mneme2 data.

    This is the canonical input format for importer endpoints.
    """

    legacy_id: str = Field(description="Original ID from Mneme2 (table:row).")
    source_type: ImportSourceType = Field(
        default=ImportSourceType.mneme2_item,
        description="Type of the source item.",
    )
    title: str = Field(min_length=1, max_length=300, description="Item title.")
    content_type: str | None = Field(
        default=None, description="Original content type / MIME."
    )
    content_text: str | None = Field(
        default=None, description="Plain-text content for text items."
    )
    content_uri: str | None = Field(
        default=None, description="URI/path to content for file items."
    )
    content_hash: str | None = Field(
        default=None, description="Pre-computed SHA-256 hash (if available)."
    )
    size_bytes: int | None = Field(
        default=None, ge=0, description="Original file size in bytes."
    )
    tags: list[str] = Field(default_factory=list, description="Original tags/labels.")
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Free-form metadata from the source system.",
    )
    created_at: datetime | None = Field(
        default=None, description="Original creation timestamp."
    )
    updated_at: datetime | None = Field(
        default=None, description="Original last-updated timestamp."
    )
    author: str | None = Field(default=None, description="Original author/creator.")
    source: str = Field(default="importer", description="Source identifier.")


class ImportPayload(ApiSchema):
    """Payload for a dry-run, preview, or import request.

    Contains the array of source items and execution parameters.
    """

    project_id: UUID = Field(description="Target project UUID for the import.")
    source_type: ImportSourceType = Field(
        default=ImportSourceType.mneme2_item,
        description="Type of source data.",
    )
    items: list[ImportSourceItem] = Field(
        min_length=1, max_length=1000,
        description="Array of source items to import (1–1000).",
    )
    dry_run: bool = Field(
        default=False,
        description="If True, validate only — no side effects.",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Validation result
# ═══════════════════════════════════════════════════════════════════════════════


class ValidationIssue(ApiSchema):
    """A single validation issue found during dry-run."""
    index: int = Field(description="0-based index of the item in the batch.")
    legacy_id: str = Field(description="Legacy ID of the problematic item.")
    field: str | None = Field(default=None, description="Field with the issue.")
    severity: str = Field(default="error", description="error | warning | info.")
    message: str = Field(description="Human-readable issue description.")


class ValidationResult(ApiSchema):
    """Result of a dry-run validation pass."""
    passed: bool = Field(description="True if all items passed validation.")
    total_items: int = Field(description="Total number of items validated.")
    valid_count: int = Field(description="Number of items that passed.")
    error_count: int = Field(description="Number of items with errors.")
    warning_count: int = Field(description="Number of warnings.")
    issues: list[ValidationIssue] = Field(
        default_factory=list, description="List of found issues."
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Preview result
# ═══════════════════════════════════════════════════════════════════════════════


class PreviewMapping(ApiSchema):
    """Preview of one source item mapped to a v4.1 asset."""
    index: int = Field(description="0-based item index.")
    legacy_id: str = Field(description="Source legacy ID.")
    target_asset: dict[str, Any] = Field(
        description="Generated AssetCreateRequest fields (before DB write)."
    )
    mapping_notes: list[str] = Field(
        default_factory=list,
        description="Notes about mapping decisions for this item.",
    )
    warnings: list[str] = Field(default_factory=list, description="Non-fatal warnings.")


class PreviewResult(ApiSchema):
    """Result of a mapping preview."""
    source_type: ImportSourceType = Field(description="Source type used.")
    mapping_version: str = Field(description="Mapping schema version.")
    total_items: int = Field(description="Number of items in the preview.")
    previews: list[PreviewMapping] = Field(
        description="Preview mappings for each item."
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Import report
# ═══════════════════════════════════════════════════════════════════════════════


class ImportItemResult(ApiSchema):
    """Result for a single imported item."""
    index: int
    legacy_id: str
    status: str = Field(description="succeeded | failed | skipped")
    asset_id: UUID | None = Field(default=None, description="Created asset UUID.")
    asset_uid: str | None = Field(default=None, description="Created asset UID.")
    error: str | None = Field(default=None, description="Error message if failed.")


class ImportReport(ApiSchema):
    """Complete import report after a formal import run."""
    run_id: UUID = Field(description="Pipeline run ID tracking this import.")
    project_id: UUID = Field(description="Target project UUID.")
    source_type: ImportSourceType = Field(description="Source type imported.")
    status: ImportStatus = Field(description="Overall run status.")
    started_at: datetime | None = Field(default=None)
    finished_at: datetime | None = Field(default=None)
    total_items: int = Field(description="Items in the batch.")
    succeeded: int = Field(default=0)
    failed: int = Field(default=0)
    skipped: int = Field(default=0)
    items: list[ImportItemResult] = Field(
        default_factory=list, description="Per-item results."
    )
    summary: str = Field(
        default="", description="Human-readable summary text."
    )

    def to_markdown(self) -> str:
        """Render the report as Markdown."""
        lines = [
            f"# Import Report — {self.run_id}",
            "",
            f"- **Project**: `{self.project_id}`",
            f"- **Source Type**: {self.source_type.value}",
            f"- **Status**: {self.status.value}",
            f"- **Total**: {self.total_items} items",
            f"- **Succeeded**: {self.succeeded}",
            f"- **Failed**: {self.failed}",
            f"- **Skipped**: {self.skipped}",
            "",
        ]
        if self.started_at:
            lines.append(f"- **Started**: {self.started_at.isoformat()}")
        if self.finished_at:
            lines.append(f"- **Finished**: {self.finished_at.isoformat()}")
        lines.append("")
        lines.append("## Summary")
        lines.append(self.summary or "No summary available.")
        if self.items:
            lines.append("")
            lines.append("## Items")
            lines.append("| # | Legacy ID | Status | Asset UID | Error |")
            lines.append("|---|-----------|--------|-----------|-------|")
            for item in self.items:
                uid = item.asset_uid or "—"
                err = item.error or "—"
                lines.append(
                    f"| {item.index} | {item.legacy_id} | {item.status} | {uid} | {err} |"
                )
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# Import run (lightweight wrapper)
# ═══════════════════════════════════════════════════════════════════════════════


class ImportRunRead(ApiSchema):
    """Read representation of an import run (backed by pipeline_runs)."""
    run_id: UUID = Field(description="Pipeline run UUID tracking this import.")
    project_id: UUID | None = Field(default=None)
    status: ImportStatus = Field(default=ImportStatus.pending)
    source_type: str | None = Field(default=None)
    mode: str = Field(default="import")
    total_items: int = Field(default=0)
    input_json: dict[str, Any] = Field(default_factory=dict)
    output_json: dict[str, Any] = Field(default_factory=dict)
    error_json: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime | None = Field(default=None)
    finished_at: datetime | None = Field(default=None)
    created_at: datetime | None = Field(default=None)


class ImportRunListResponse(ApiSchema):
    """Paginated list of import runs."""
    items: list[ImportRunRead]
    page_info: PageInfo


class ImportRunFilterParams(PaginationParams):
    """Filter parameters for listing import runs."""
    status: ImportStatus | None = None
    project_id: UUID | None = None
    source_type: str | None = None
