"""Pydantic schemas for database migration management.

Provides request/response models for the migration admin API, covering:
* Migration listing (paginated)
* Migration detail
* Migration creation / application
* Migration rollback
* Migration preview (dry-run)
* Migration status tracking
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import Field

from mneme.schemas.common import ApiSchema, PageInfo, PaginatedData, PaginationParams


# ═══════════════════════════════════════════════════════════════════════════════
# Enums
# ═══════════════════════════════════════════════════════════════════════════════


class MigrationStatus(str, Enum):
    """Status of a migration revision."""
    pending = "pending"
    applied = "applied"
    rolled_back = "rolled_back"
    failed = "failed"
    skipped = "skipped"


class MigrationDirection(str, Enum):
    """Direction of migration: upgrade or downgrade."""
    upgrade = "upgrade"
    downgrade = "downgrade"


# ═══════════════════════════════════════════════════════════════════════════════
# Migration revision (individual)
# ═══════════════════════════════════════════════════════════════════════════════


class MigrationRevisionRead(ApiSchema):
    """Read representation of a single Alembic migration revision."""

    revision_id: str = Field(description="Alembic revision identifier (e.g. 'a1b2c3d4e5f6').")
    down_revision: str | list[str] | None = Field(
        default=None,
        description="Parent revision ID(s). A list indicates a merge revision with multiple parents.",
    )
    message: str | None = Field(
        default=None,
        description="Migration docstring / message.",
    )
    status: MigrationStatus = Field(
        default=MigrationStatus.pending,
        description="Current status in the database.",
    )
    applied_at: datetime | None = Field(
        default=None,
        description="Timestamp when this migration was applied.",
    )
    applied_by: str | None = Field(
        default=None,
        description="Actor who applied this migration.",
    )
    file_name: str | None = Field(
        default=None,
        description="Migration script file name.",
    )


class MigrationRevisionListResponse(PaginatedData[MigrationRevisionRead]):
    """Paginated list of migration revisions."""
    pass


# ═══════════════════════════════════════════════════════════════════════════════
# Migration run (execution record)
# ═══════════════════════════════════════════════════════════════════════════════


class MigrationRunRead(ApiSchema):
    """Read representation of a migration execution run."""

    run_id: UUID = Field(description="Unique run identifier.")
    direction: MigrationDirection = Field(
        default=MigrationDirection.upgrade,
        description="Whether this run applied or rolled back migrations.",
    )
    target_revision: str | None = Field(
        default=None,
        description="Target revision for this run ('head', specific rev, etc.).",
    )
    status: MigrationStatus = Field(
        default=MigrationStatus.pending,
        description="Overall run status.",
    )
    revisions_applied: list[str] = Field(
        default_factory=list,
        description="List of revision IDs applied in this run.",
    )
    revisions_failed: list[str] = Field(
        default_factory=list,
        description="List of revision IDs that failed in this run.",
    )
    error_message: str | None = Field(
        default=None,
        description="Error message if the run failed.",
    )
    started_at: datetime | None = Field(default=None)
    finished_at: datetime | None = Field(default=None)
    created_at: datetime | None = Field(default=None)
    triggered_by: str | None = Field(
        default=None,
        description="Actor who triggered this run.",
    )


class MigrationRunListResponse(PaginatedData[MigrationRunRead]):
    """Paginated list of migration runs."""
    pass


# ═══════════════════════════════════════════════════════════════════════════════
# Migration state summary
# ═══════════════════════════════════════════════════════════════════════════════


class MigrationStateSummary(ApiSchema):
    """Summary of the current migration state of the database."""

    current_head: str | None = Field(
        default=None,
        description="Current HEAD revision ID.",
    )
    latest_applied: str | None = Field(
        default=None,
        description="Latest applied revision ID in the database.",
    )
    total_revisions: int = Field(
        default=0,
        description="Total number of known revisions.",
    )
    applied_count: int = Field(
        default=0,
        description="Number of applied revisions.",
    )
    pending_count: int = Field(
        default=0,
        description="Number of pending (unapplied) revisions.",
    )
    failed_count: int = Field(
        default=0,
        description="Number of failed revisions.",
    )
    is_up_to_date: bool = Field(
        default=False,
        description="True if the database is at the latest revision.",
    )
    latest_revision_info: MigrationRevisionRead | None = Field(
        default=None,
        description="Info about the latest available revision.",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Request models
# ═══════════════════════════════════════════════════════════════════════════════


class MigrationApplyRequest(ApiSchema):
    """Request to apply pending migrations.

    Supports targeting a specific revision or applying all pending
    migrations up to 'head'.
    """

    target_revision: str | None = Field(
        default=None,
        description=(
            "Target revision to upgrade to. If omitted, migrates to 'head' "
            "(latest). Specify a partial revision prefix or full revision ID."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description="If True, preview which migrations would be applied without executing.",
    )
    sql_only: bool = Field(
        default=False,
        description="If True, output the SQL that would be executed instead of running it.",
    )


class MigrationRollbackRequest(ApiSchema):
    """Request to rollback applied migrations.

    Rolls back to the specified target revision (downgrade).
    """

    target_revision: str = Field(
        description=(
            "Revision to downgrade to. Use '-1' for one step back, or a "
            "specific revision ID."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description="If True, preview the rollback without executing.",
    )


class MigrationPreviewResponse(ApiSchema):
    """Response for a migration preview (dry-run)."""

    direction: MigrationDirection = Field(
        description="Direction of the migration (upgrade/downgrade).",
    )
    from_revision: str = Field(
        description="Starting revision ID.",
    )
    to_revision: str = Field(
        description="Target revision ID.",
    )
    revisions_to_apply: list[str] = Field(
        default_factory=list,
        description="Revision IDs that would be applied.",
    )
    sql_statements: list[str] = Field(
        default_factory=list,
        description="Raw SQL statements that would be executed (if sql_only=True).",
    )
    total_steps: int = Field(
        default=0,
        description="Number of migration steps that would be executed.",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Any warnings about the pending migrations.",
    )


class MigrationApplyResponse(ApiSchema):
    """Response after applying or rolling back migrations."""

    run_id: UUID = Field(description="Migration run ID tracking this execution.")
    direction: MigrationDirection = Field(
        description="Direction of the migration run.",
    )
    status: MigrationStatus = Field(
        description="Overall status after the run.",
    )
    revisions_applied: list[str] = Field(
        default_factory=list,
        description="Revisions successfully applied/rolled back.",
    )
    revisions_failed: list[str] = Field(
        default_factory=list,
        description="Revisions that failed.",
    )
    message: str = Field(
        default="Migration completed",
        description="Human-readable status message.",
    )
    error_message: str | None = Field(
        default=None,
        description="Error details if the run failed.",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Filter params
# ═══════════════════════════════════════════════════════════════════════════════


class MigrationFilterParams(PaginationParams):
    """Filter parameters for listing migration revisions."""
    status: MigrationStatus | None = None
    search: str | None = Field(
        default=None,
        description="Search by revision ID prefix or message text.",
    )


class MigrationRunFilterParams(PaginationParams):
    """Filter parameters for listing migration runs."""
    status: MigrationStatus | None = None
    direction: MigrationDirection | None = None
