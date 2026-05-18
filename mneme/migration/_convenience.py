"""P5-04 High-level convenience wrappers for the migration module.

Provides top-level orchestration functions that compose the lower-level
discovery → plan → dump → load → verify → track pipeline into simple
one-call operations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from mneme.migration.discovery import discover_schema
from mneme.migration.dumper import dump_table
from mneme.migration.loader import load_batch
from mneme.migration.manifest import generate_run_id
from mneme.migration.planner import build_plan, MigrationPlan
from mneme.migration.tracker import (
    RunMode,
    RunStatus,
    TableStatus,
    start_run,
    complete_run,
    fail_run,
)
from mneme.migration.verifier import verify_counts, verify_hashes


@dataclass
class MigrationReport:
    """Composite report of a migration run.

    Combines the migration plan with verification results and run status.
    """

    run_id: UUID
    run_status: str
    plan: MigrationPlan
    tables_migrated: int = 0
    tables_failed: int = 0
    rows_total: int = 0
    rows_loaded: int = 0
    count_verified: int = 0
    hash_verified: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)
    duration_seconds: float = 0.0


def run_migration(
    db: Session,
    *,
    source_db_path: str,
    mode: str = "formal",
    target_schema: str = "public",
    batch_size: int = 500,
) -> MigrationReport:
    """Execute a migration end-to-end.

    This is the top-level entry point for running a complete migration
    from a SQLite source to the PostgreSQL target.

    Parameters
    ----------
    db : Session
        Target PostgreSQL session.
    source_db_path : str
        Path to the source SQLite database file.
    mode : str
        One of ``"dry_run"``, ``"shadow"``, ``"formal"``.
    target_schema : str
        Target PostgreSQL schema name.
    batch_size : int
        Number of rows per batch for loading.

    Returns
    -------
    MigrationReport
        Composite report of the migration run.
    """
    import time
    start_time = time.monotonic()

    run_mode = RunMode(mode) if mode in {"dry_run", "shadow", "formal"} else RunMode.DRY_RUN
    run_id = generate_run_id()

    errors: list[dict[str, Any]] = []
    tables_migrated = 0
    tables_failed = 0
    rows_total = 0
    rows_loaded = 0

    try:
        # 1. Discover source schema
        schema = discover_schema(source_db_path)

        # 2. Build migration plan
        plan = build_plan(schema, target_schema=target_schema)

        # 3. Start tracking run
        start_run(db, run_id=run_id, mode=run_mode, plan=plan)

        # 4. Migrate each table
        for table_plan in plan.tables:
            try:
                for batch in dump_table(
                    source_db_path,
                    table_name=table_plan.source_table,
                    batch_size=batch_size,
                ):
                    rows_total += len(batch)
                    result = load_batch(
                        db,
                        batch=batch,
                        table_plan=table_plan,
                        run_mode=run_mode,
                    )
                    rows_loaded += result.loaded_count

                tables_migrated += 1

                # 5. Verify counts
                verify_counts(
                    db,
                    table_name=table_plan.target_table,
                    expected_count=table_plan.source_row_count or 0,
                )

                # 6. Verify hashes (if applicable)
                verify_hashes(
                    db,
                    source_db_path=source_db_path,
                    table_plan=table_plan,
                )

            except Exception as exc:
                tables_failed += 1
                errors.append({
                    "table": table_plan.source_table,
                    "error": str(exc),
                })

        # 7. Complete run
        complete_run(db, run_id=run_id)

    except Exception as exc:
        fail_run(db, run_id=run_id, error=str(exc))
        errors.append({"phase": "orchestration", "error": str(exc)})
        plan = MigrationPlan(tables=[])

    duration = time.monotonic() - start_time

    return MigrationReport(
        run_id=run_id,
        run_status="completed" if tables_failed == 0 else "partial",
        plan=plan,
        tables_migrated=tables_migrated,
        tables_failed=tables_failed,
        rows_total=rows_total,
        rows_loaded=rows_loaded,
        errors=errors,
        duration_seconds=duration,
    )
