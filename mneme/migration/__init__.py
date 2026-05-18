"""Migration module — Mneme-legacy (SQLite) → Mneme-v4 (PostgreSQL) migration engine.

Provides a complete migration pipeline:

1. **Discovery** — scan source SQLite schema
2. **Planning** — build migration plan with field mappings
3. **Dumping** — stream rows from source in batches
4. **Loading** — transform and UPSERT into target PG
5. **Verification** — row-count and hash-based integrity checks
6. **Recovery** — checkpoint-based rollback
7. **Tracking** — per-table progress and run history

Public API
----------
* ``run_migration()`` — execute a complete migration end-to-end
* ``dry_run_migration()`` — preview migration without writing
* ``shadow_migration()`` — migrate to temporary shadow tables
* ``rollback_migration()`` — revert to a checkpoint
* ``generate_report()`` — produce a migration report
"""

from __future__ import annotations

from mneme.migration.discovery import (
    ColumnInfo,
    SourceSchema,
    TableInfo,
    discover_schema,
    discover_table_info,
)
from mneme.migration.dumper import (
    dump_row_count,
    dump_single_row,
    dump_table,
    dump_table_with_offset,
)
from mneme.migration.loader import (
    LoadResult,
    create_shadow_table,
    drop_shadow_table,
    load_batch,
)
from mneme.migration.manifest import (
    COLUMN_MAP,
    ENUM_MAP,
    MIGRATION_ORDER,
    MIGRATION_VERSION,
    NEW_COLUMN_DEFAULTS,
    TABLE_MAP,
    generate_run_id,
)
from mneme.migration.planner import (
    ColumnTransform,
    MigrationPlan,
    TablePlan,
    build_plan,
    build_table_plan,
)
from mneme.migration.recovery import (
    CheckPoint,
    RollbackResult,
    cleanup_checkpoints,
    create_checkpoint,
    get_checkpoint,
    list_checkpoints,
    rollback_to_checkpoint,
)
from mneme.migration.tracker import (
    MigrationRun,
    RunMode,
    RunStatus,
    TableProgress,
    TableStatus,
    complete_run,
    fail_run,
    get_run,
    list_runs,
    start_run,
)
from mneme.migration.verifier import (
    CountVerifyResult,
    HashVerifyResult,
    verify_counts,
    verify_hashes,
)
from mneme.migration._convenience import (
    MigrationReport,
    run_migration,
)

__all__ = [
    # discovery
    "ColumnInfo",
    "SourceSchema",
    "TableInfo",
    "discover_schema",
    "discover_table_info",
    # dumper
    "dump_row_count",
    "dump_single_row",
    "dump_table",
    "dump_table_with_offset",
    # loader
    "LoadResult",
    "create_shadow_table",
    "drop_shadow_table",
    "load_batch",
    # manifest
    "COLUMN_MAP",
    "ENUM_MAP",
    "MIGRATION_ORDER",
    "MIGRATION_VERSION",
    "NEW_COLUMN_DEFAULTS",
    "TABLE_MAP",
    "generate_run_id",
    # planner
    "ColumnTransform",
    "MigrationPlan",
    "TablePlan",
    "build_plan",
    "build_table_plan",
    # recovery
    "CheckPoint",
    "RollbackResult",
    "cleanup_checkpoints",
    "create_checkpoint",
    "get_checkpoint",
    "list_checkpoints",
    "rollback_to_checkpoint",
    # tracker
    "MigrationRun",
    "RunMode",
    "RunStatus",
    "TableProgress",
    "TableStatus",
    "complete_run",
    "fail_run",
    "get_run",
    "list_runs",
    "start_run",
    # verifier
    "CountVerifyResult",
    "HashVerifyResult",
    "verify_counts",
    "verify_hashes",
    # convenience (P5-04)
    "MigrationReport",
    "run_migration",
]
