"""P2-16 Restore preview — compare backup manifest against live DB before restore.

Provides a preview of what the restore will affect:
* Table count comparison
* Row count comparison per table
* Identification of tables that would be created, overwritten, or missing
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mneme.backup.engine import _default_backup_root, _get_table_row_counts
from mneme.backup.pg_dump import parse_db_url
from mneme.backup.manifest import (
    BackupManifest,
    find_all_manifests,
    load_manifest,
)
from mneme.config import get_settings

logger = logging.getLogger("mneme.restore.preview")


@dataclass
class TableComparison:
    """Per-table comparison between backup manifest and live DB."""

    table_name: str
    backup_rows: int = 0
    live_rows: int = 0
    difference: int = 0
    exists_in_live: bool = True
    will_be: str = "unchanged"  # one of: unchanged, overwritten, created, missing_in_backup


@dataclass
class RestorePreview:
    """Preview of what a restore operation will do."""

    backup_id: str
    backup_created_at: str = ""
    backup_tables: int = 0
    live_tables: int = 0
    table_comparisons: list[TableComparison] = field(default_factory=list)
    total_rows_backup: int = 0
    total_rows_live: int = 0
    will_overwrite_tables: int = 0
    will_create_tables: int = 0
    will_drop_tables: int = 0
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "backup_id": self.backup_id,
            "backup_created_at": self.backup_created_at,
            "backup_tables": self.backup_tables,
            "live_tables": self.live_tables,
            "table_comparisons": [
                {
                    "table_name": tc.table_name,
                    "backup_rows": tc.backup_rows,
                    "live_rows": tc.live_rows,
                    "difference": tc.difference,
                    "exists_in_live": tc.exists_in_live,
                    "will_be": tc.will_be,
                }
                for tc in self.table_comparisons
            ],
            "total_rows_backup": self.total_rows_backup,
            "total_rows_live": self.total_rows_live,
            "will_overwrite_tables": self.will_overwrite_tables,
            "will_create_tables": self.will_create_tables,
            "will_drop_tables": self.will_drop_tables,
            "warnings": self.warnings,
            "error": self.error,
        }


def preview_restore(
    backup_id: str,
    *,
    backup_root: Path | None = None,
    target_database_url: str | None = None,
) -> RestorePreview:
    """Generate a restore preview comparing a backup manifest against the live DB.

    Parameters
    ----------
    backup_id : str
        UUID string of the backup to preview.
    backup_root : Path | None
        Root directory for backups. If None, uses the default.
    target_database_url : str | None
        Target database URL for comparison. If None, uses settings.

    Returns
    -------
    RestorePreview with per-table comparisons and summary statistics.
    """
    if backup_root is None:
        backup_root = _default_backup_root()

    # 1. Find the backup manifest
    manifest = _find_backup_manifest(backup_id, backup_root)
    if manifest is None:
        return RestorePreview(
            backup_id=backup_id,
            error=f"Backup '{backup_id}' not found",
        )

    # 2. Validate the dump file exists
    dump_path = Path(manifest.file_path)
    if not dump_path.exists():
        return RestorePreview(
            backup_id=backup_id,
            backup_created_at=manifest.created_at,
            backup_tables=manifest.tables,
            error=f"Dump file not found: {dump_path}",
        )

    # 3. Get live DB row counts
    try:
        if target_database_url:
            db_params = parse_db_url(target_database_url)
        else:
            db_params = parse_db_url(get_settings().database_url)
    except Exception as exc:
        return RestorePreview(
            backup_id=backup_id,
            backup_created_at=manifest.created_at,
            backup_tables=manifest.tables,
            error=f"Database connection error: {exc}",
        )

    live_counts = {}
    try:
        live_counts = _get_table_row_counts(db_params)
    except Exception as exc:
        logger.warning("Could not get live row counts: %s", exc)

    # 4. Build per-table comparisons
    backup_counts = manifest.table_row_counts
    all_table_names = sorted(
        set(list(backup_counts.keys()) + list(live_counts.keys()))
    )

    comparisons: list[TableComparison] = []
    total_backup = 0
    total_live = 0
    will_overwrite = 0
    will_create = 0
    will_drop = 0

    for tbl in all_table_names:
        backup_rows = backup_counts.get(tbl, 0)
        live_rows = live_counts.get(tbl, 0)
        exists = tbl in live_counts

        if backup_rows > 0 and not exists:
            will_be = "created"
            will_create += 1
        elif backup_rows > 0 and exists:
            will_be = "overwritten" if backup_rows != live_rows else "unchanged"
            if backup_rows != live_rows:
                will_overwrite += 1
        elif backup_rows == 0 and exists:
            will_be = "missing_in_backup"
            will_drop += 1
        else:
            will_be = "unchanged"

        comparisons.append(TableComparison(
            table_name=tbl,
            backup_rows=backup_rows,
            live_rows=live_rows,
            difference=backup_rows - live_rows,
            exists_in_live=exists,
            will_be=will_be,
        ))

        total_backup += backup_rows
        total_live += live_rows

    # 5. Generate warnings
    warnings: list[str] = []

    if will_overwrite > 0:
        warnings.append(
            f"{will_overwrite} table(s) will have different row counts after restore"
        )
    if will_create > 0:
        warnings.append(
            f"{will_create} table(s) will be created (not in live DB)"
        )
    if will_drop > 0:
        warnings.append(
            f"{will_drop} table(s) exist in live DB but have no rows in backup — "
            "they will be dropped if --clean is used"
        )

    if not live_counts:
        warnings.append("Could not compare against live DB (may be empty or unreachable)")

    if manifest.tables != len(live_counts):
        warnings.append(
            f"Backup has {manifest.tables} tables, live DB has {len(live_counts)} tables"
        )

    return RestorePreview(
        backup_id=backup_id,
        backup_created_at=manifest.created_at,
        backup_tables=manifest.tables,
        live_tables=len(live_counts),
        table_comparisons=comparisons,
        total_rows_backup=total_backup,
        total_rows_live=total_live,
        will_overwrite_tables=will_overwrite,
        will_create_tables=will_create,
        will_drop_tables=will_drop,
        warnings=warnings,
    )


def _find_backup_manifest(
    backup_id: str,
    backup_root: Path,
) -> BackupManifest | None:
    """Find a backup manifest by backup_id in the backup root."""
    if not backup_root.exists():
        return None
    try:
        for entry in sorted(backup_root.iterdir(), reverse=True):
            if entry.is_dir():
                m = load_manifest(entry)
                if m is not None and m.backup_id == backup_id:
                    return m
    except (FileNotFoundError, PermissionError):
        return None
    return None
