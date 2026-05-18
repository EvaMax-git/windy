"""Mneme Backup v1 — pg_dump + manifest + integrity verification + restore.

Package contents
----------------
* ``pg_dump``        – ``pg_dump()``, ``pg_restore()``, ``parse_db_url()``, ``check_pg_tools()``
* ``engine``         – ``run_backup()``, ``verify_backup()``, ``list_backups()``
* ``restore_engine`` – ``run_restore_drill()``, ``run_restore_live()``, ``RestoreReport``
* ``manifest``       – ``BackupManifest``, save/load, validation, checksum
* ``cli``            – command-line interface (``python -m mneme.backup``)
"""

from mneme.backup.engine import (
    BackupResult,
    list_backups,
    run_backup,
    verify_backup,
)
from mneme.backup.manifest import (
    BackupManifest,
    compute_sha256,
    find_all_manifests,
    load_manifest,
    save_manifest,
    validate_manifest,
    verify_checksum,
)
from mneme.backup.pg_dump import (
    DbConnectionParams,
    DumpFormat,
    PgDumpResult,
    PgRestoreResult,
    check_pg_tools,
    parse_db_url,
    pg_dump,
    pg_restore,
)
from mneme.backup.restore_engine import (
    RestoreReport,
    RestoreResult,
    load_restore_report,
    find_all_restore_reports,
    list_restores,
    run_restore_drill,
    run_restore_live,
)

__all__ = [
    "BackupManifest",
    "BackupResult",
    "check_pg_tools",
    "compute_sha256",
    "DbConnectionParams",
    "DumpFormat",
    "find_all_manifests",
    "find_all_restore_reports",
    "list_backups",
    "list_restores",
    "load_manifest",
    "load_restore_report",
    "parse_db_url",
    "PgDumpResult",
    "PgRestoreResult",
    "pg_dump",
    "pg_restore",
    "RestoreReport",
    "RestoreResult",
    "run_backup",
    "run_restore_drill",
    "run_restore_live",
    "save_manifest",
    "validate_manifest",
    "verify_backup",
    "verify_checksum",
]
