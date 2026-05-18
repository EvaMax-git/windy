"""P2-14 / P2-15 / P2-16 Backup & Restore schemas — API request/response models."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import Field

from mneme.schemas.common import ApiSchema, PageInfo, PaginatedData


# ── Backup summary (for list responses) ──────────────────────────────────────


class BackupSummary(ApiSchema):
    """Summary of a single backup for list responses."""

    backup_id: str = Field(description="UUID of the backup")
    created_at: str = Field(description="ISO 8601 creation timestamp")
    pg_version: str = Field(description="PostgreSQL version at backup time")
    status: str = Field(description="Backup status: succeeded, failed, in_progress")
    file_size_bytes: int = Field(description="Size of the dump file in bytes", ge=0)
    alembic_revision: str = Field(description="Alembic revision at backup time")
    tables: int = Field(description="Number of tables in the backup")
    table_count_summary: dict[str, int] = Field(
        default_factory=dict,
        description="Summary: total_rows, non_empty_tables",
    )
    checksum_sha256: str = Field(
        default="",
        description="First 16 chars of SHA-256 checksum + '...'",
    )
    backup_directory: str = Field(description="Absolute path to backup directory")


class BackupListResponse(PaginatedData[BackupSummary]):
    """Paginated list of backups."""
    pass


# ── Backup detail (for single-backup responses) ──────────────────────────────


class BackupDetail(ApiSchema):
    """Full detail of a single backup (all manifest fields)."""

    backup_id: str
    created_at: str
    pg_version: str
    format: str = "custom"
    tables: int = 45
    table_row_counts: dict[str, int] = Field(default_factory=dict)
    file_path: str
    file_size_bytes: int = 0
    checksum_sha256: str = ""
    alembic_revision: str = ""
    status: str = "succeeded"
    error_message: str | None = None
    completed_at: str | None = None
    dump_command: str | None = None
    env_info: dict[str, str] = Field(default_factory=dict)


# ── Verification response ────────────────────────────────────────────────────


class BackupVerifyResponse(ApiSchema):
    """Response for a backup integrity verification."""

    backup_id: str
    valid: bool
    issues: list[str] = Field(default_factory=list)
    file_size_bytes: int | None = None
    checksum_match: bool | None = None


# ── P2-15 Restore schemas ────────────────────────────────────────────────────


class RestoreVerificationResult(ApiSchema):
    """Individual verification check result within a restore report."""

    table_count: dict[str, Any] = Field(
        default_factory=dict,
        description="{expected, actual, match}",
    )
    row_counts: dict[str, Any] = Field(
        default_factory=dict,
        description="{match, mismatches, manifest_tables_not_in_restored, restored_tables_not_in_manifest}",
    )
    foreign_keys: dict[str, Any] = Field(
        default_factory=dict,
        description="{valid, violations}",
    )
    alembic_revision: dict[str, Any] = Field(
        default_factory=dict,
        description="{expected, actual, match}",
    )


class RestoreSourceInfo(ApiSchema):
    """Source backup info within a restore report."""

    backup_id: str
    created_at: str = ""
    file_path: str = ""
    file_size_bytes: int = 0
    checksum_sha256: str = ""


class RestoreReportDetail(ApiSchema):
    """Full detail of a single restore report."""

    restore_id: str
    backup_id: str
    restore_type: str = Field(description="drill or live")
    started_at: str
    completed_at: str = ""
    status: str = Field(description="succeeded, failed, in_progress")
    target_database: str = ""
    source_backup: RestoreSourceInfo = Field(default_factory=RestoreSourceInfo)
    verification: RestoreVerificationResult = Field(
        default_factory=RestoreVerificationResult
    )
    error_message: str | None = None


class RestoreSummary(ApiSchema):
    """Summary of a single restore report for list responses."""

    restore_id: str
    backup_id: str
    restore_type: str
    status: str
    started_at: str
    completed_at: str = ""
    target_database: str = ""
    report_directory: str = ""


class RestoreListResponse(PaginatedData[RestoreSummary]):
    """Paginated list of restore reports."""
    pass


class RestoreDrillRequest(ApiSchema):
    """Request to execute a restore drill."""

    backup_id: str = Field(description="UUID of the backup to restore from")
    target_database_url: str | None = Field(
        default=None,
        description="Optional explicit target DB URL. If omitted, a temp DB is created.",
    )
    keep_temp_db: bool = Field(
        default=False,
        description="If true, the temporary database is kept after the drill.",
    )


class RestoreDrillResponse(ApiSchema):
    """Response after a restore drill is attempted."""

    restore_id: str
    success: bool
    status: str
    verification_summary: dict[str, bool] = Field(
        default_factory=dict,
        description="High-level pass/fail for each verification category",
    )
    report_path: str = ""
    error_message: str | None = None


class RestorePreviewResponse(ApiSchema):
    """Preview of what a restore would look like (P2-16 pre-restore check)."""

    backup_id: str
    created_at: str = ""
    tables: int = 45
    table_row_counts: dict[str, int] = Field(default_factory=dict)
    file_size_bytes: int = 0
    target_database: str = ""
    restore_type: str = "drill"


# ── P2-16: Trigger backup ──────────────────────────────────────────────────────


class BackupTriggerRequest(ApiSchema):
    """Request body for ``POST /admin/backup`` — trigger a new backup job."""

    database_url: str | None = Field(
        default=None,
        description="Override the PostgreSQL connection URL. Uses settings if omitted.",
    )
    backup_id: str | None = Field(
        default=None,
        description="Custom UUID for the backup. Auto-generated if omitted.",
    )


class BackupTriggerResponse(ApiSchema):
    """Response after triggering a backup job."""

    backup_id: str = Field(description="UUID of the backup")
    job_id: UUID = Field(description="Job ID tracking the backup execution")
    status: str = Field(description="Initial job status (always 'pending')")
    message: str = Field(
        default="Backup job created",
        description="Human-readable status message",
    )


# ── P2-16: Restore request (via Review) ─────────────────────────────────────────


class RestoreSubmitRequest(ApiSchema):
    """Request body for ``POST /admin/restore`` — submit a restore request.

    Always creates a review_item with ``review_type='restore_confirm'``.
    """

    backup_id: str = Field(description="UUID of the backup to restore")
    target_database_url: str | None = Field(
        default=None,
        description="Override the target database URL. Uses settings if omitted.",
    )
    clean: bool = Field(
        default=True,
        description="If True, passes --clean --if-exists to pg_restore.",
    )
    reason: str | None = Field(
        default=None,
        description="Justification for the restore request.",
    )


class RestoreSubmitResponse(ApiSchema):
    """Response after submitting a restore request."""

    backup_id: str = Field(description="UUID of the backup to restore")
    review_item_id: UUID = Field(description="Review item ID for the restore request")
    status: str = Field(description="Review status (always 'pending' initially)")
    message: str = Field(
        default="Restore request submitted. Awaiting review approval.",
        description="Human-readable status message",
    )


# ── P2-16: Restore detailed preview ────────────────────────────────────────────


class TableComparisonItem(ApiSchema):
    """Per-table comparison in a restore preview."""

    table_name: str
    backup_rows: int = 0
    live_rows: int = 0
    difference: int = 0
    exists_in_live: bool = True
    will_be: str = Field(
        default="unchanged",
        description="One of: unchanged, overwritten, created, missing_in_backup",
    )


class RestoreDetailedPreview(ApiSchema):
    """Detailed restore preview with per-table comparison."""

    backup_id: str
    backup_created_at: str = ""
    backup_tables: int = 0
    live_tables: int = 0
    table_comparisons: list[TableComparisonItem] = Field(default_factory=list)
    total_rows_backup: int = 0
    total_rows_live: int = 0
    will_overwrite_tables: int = 0
    will_create_tables: int = 0
    will_drop_tables: int = 0
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None


# ── P2-16: Job status tracking for backup/restore ──────────────────────────────


class JobLogEntry(ApiSchema):
    """A single job log entry."""

    job_log_id: UUID
    job_id: UUID
    step: str
    level: str = "info"
    message: str
    attempt_no: int = 0
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    occurred_at: str | None = None


class JobSummary(ApiSchema):
    """Summary of a single job for list responses."""

    job_id: UUID
    job_key: str
    job_type: str
    status: str
    project_id: UUID | str | None = None
    priority: int = 100
    queue_name: str = "default"
    scheduled_at: str | None = None
    available_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    lease_owner: str | None = None
    lease_expires_at: str | None = None
    idempotency_key: str | None = None
    retry_count: int = 0
    max_retries: int = 0
    timeout_seconds: int = 3600
    cause_event_id: UUID | str | None = None
    aggregate_type: str | None = None
    aggregate_id: UUID | str | None = None
    target_version: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] = Field(default_factory=dict)
    last_error: str | None = None
    created_by_actor_type: str = "system"
    created_by_actor_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class JobListResponse(PaginatedData[JobSummary]):
    """Paginated list of jobs."""
    pass


class JobStatusResponse(ApiSchema):
    """Response for a backup/restore job status query."""

    job_id: UUID
    job_type: str
    job_key: str
    status: str
    priority: int = 100
    queue_name: str = "default"
    scheduled_at: str | None = None
    available_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    retry_count: int = 0
    max_retries: int = 0
    input_payload: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] = Field(default_factory=dict)
    last_error: str | None = None
    logs: list[JobLogEntry] = Field(default_factory=list)
