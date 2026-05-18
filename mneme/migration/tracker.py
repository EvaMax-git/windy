"""Migration progress tracker — records and reports per-table migration status.

Tracks the lifecycle of a migration run: which tables have been processed,
timing information, row counts, and any errors encountered.

Design
------
* ``MigrationRun`` — top-level run record (run_id, mode, status, timestamps)
* ``TableProgress`` — per-table progress (status, rows_dumped, rows_loaded, duration)
* ``start_run()`` / ``complete_run()`` / ``fail_run()`` — lifecycle methods
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from mneme.migration.manifest import generate_run_id


# ═══════════════════════════════════════════════════════════════════════════════
# Enums
# ═══════════════════════════════════════════════════════════════════════════════


class RunStatus(str, Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    rolled_back = "rolled_back"


class TableStatus(str, Enum):
    pending = "pending"
    in_progress = "in_progress"
    completed = "completed"
    failed = "failed"
    skipped = "skipped"


class RunMode(str, Enum):
    dry_run = "dry_run"
    shadow = "shadow"
    formal = "formal"


# ═══════════════════════════════════════════════════════════════════════════════
# Data types
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class TableProgress:
    """Per-table tracking record."""

    source_table: str
    target_table: str
    status: TableStatus = TableStatus.pending
    rows_dumped: int = 0
    rows_loaded: int = 0
    rows_skipped: int = 0
    started_at: str | None = None
    completed_at: str | None = None
    duration_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)


@dataclass
class MigrationRun:
    """Top-level migration run tracking record."""

    run_id: str
    mode: RunMode = RunMode.formal
    status: RunStatus = RunStatus.pending
    source_path: str = ""
    migration_version: str = ""
    tables: dict[str, TableProgress] = field(default_factory=dict)
    total_tables: int = 0
    completed_tables: int = 0
    failed_tables: int = 0
    total_rows_dumped: int = 0
    total_rows_loaded: int = 0
    started_at: str | None = None
    completed_at: str | None = None
    duration_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def progress_pct(self) -> float:
        if self.total_tables == 0:
            return 0.0
        return (self.completed_tables / self.total_tables) * 100.0


# ═══════════════════════════════════════════════════════════════════════════════
# In-memory run store (production would persist to DB)
# ═══════════════════════════════════════════════════════════════════════════════

_runs: dict[str, MigrationRun] = {}


# ═══════════════════════════════════════════════════════════════════════════════
# Public API — run lifecycle
# ═══════════════════════════════════════════════════════════════════════════════


def start_run(
    *,
    mode: RunMode = RunMode.formal,
    source_path: str = "",
    migration_version: str = "",
    total_tables: int = 0,
    metadata: dict[str, Any] | None = None,
) -> MigrationRun:
    """Create and start a new migration run."""
    run = MigrationRun(
        run_id=generate_run_id(),
        mode=mode,
        status=RunStatus.running,
        source_path=source_path,
        migration_version=migration_version,
        total_tables=total_tables,
        started_at=datetime.now(timezone.utc).isoformat(),
        metadata=metadata or {},
    )
    _runs[run.run_id] = run
    return run


def complete_run(run_id: str) -> MigrationRun | None:
    """Mark a migration run as successfully completed."""
    run = _runs.get(run_id)
    if run is None:
        return None
    run.status = RunStatus.completed
    run.completed_at = datetime.now(timezone.utc).isoformat()
    if run.started_at:
        start_dt = datetime.fromisoformat(run.started_at)
        run.duration_seconds = (datetime.now(timezone.utc) - start_dt).total_seconds()
    return run


def fail_run(run_id: str, error: str) -> MigrationRun | None:
    """Mark a migration run as failed."""
    run = _runs.get(run_id)
    if run is None:
        return None
    run.status = RunStatus.failed
    run.errors.append(error)
    run.completed_at = datetime.now(timezone.utc).isoformat()
    if run.started_at:
        start_dt = datetime.fromisoformat(run.started_at)
        run.duration_seconds = (datetime.now(timezone.utc) - start_dt).total_seconds()
    return run


def mark_rolled_back(run_id: str) -> MigrationRun | None:
    """Mark a migration run as rolled back."""
    run = _runs.get(run_id)
    if run is None:
        return None
    run.status = RunStatus.rolled_back
    return run


def get_run(run_id: str) -> MigrationRun | None:
    """Retrieve a migration run by ID."""
    return _runs.get(run_id)


def list_runs(
    *,
    limit: int = 20,
    offset: int = 0,
) -> list[MigrationRun]:
    """List migration runs, newest first."""
    sorted_runs = sorted(
        _runs.values(),
        key=lambda r: r.started_at or "",
        reverse=True,
    )
    return sorted_runs[offset : offset + limit]


# ═══════════════════════════════════════════════════════════════════════════════
# Public API — table tracking
# ═══════════════════════════════════════════════════════════════════════════════


def init_table_progress(
    run_id: str,
    source_table: str,
    target_table: str,
) -> TableProgress | None:
    """Register a table for tracking in the given run."""
    run = _runs.get(run_id)
    if run is None:
        return None
    tp = TableProgress(
        source_table=source_table,
        target_table=target_table,
    )
    run.tables[source_table] = tp
    return tp


def start_table(run_id: str, source_table: str) -> TableProgress | None:
    """Mark a table migration as in-progress."""
    run = _runs.get(run_id)
    if run is None:
        return None
    tp = run.tables.get(source_table)
    if tp is None:
        return None
    tp.status = TableStatus.in_progress
    tp.started_at = datetime.now(timezone.utc).isoformat()
    return tp


def complete_table(
    run_id: str,
    source_table: str,
    *,
    rows_dumped: int = 0,
    rows_loaded: int = 0,
    rows_skipped: int = 0,
) -> TableProgress | None:
    """Mark a table migration as completed with stats."""
    run = _runs.get(run_id)
    if run is None:
        return None
    tp = run.tables.get(source_table)
    if tp is None:
        return None
    tp.status = TableStatus.completed
    tp.rows_dumped = rows_dumped
    tp.rows_loaded = rows_loaded
    tp.rows_skipped = rows_skipped
    tp.completed_at = datetime.now(timezone.utc).isoformat()
    if tp.started_at:
        start_dt = datetime.fromisoformat(tp.started_at)
        tp.duration_seconds = (datetime.now(timezone.utc) - start_dt).total_seconds()
    run.completed_tables += 1
    run.total_rows_dumped += rows_dumped
    run.total_rows_loaded += rows_loaded
    return tp


def fail_table(
    run_id: str,
    source_table: str,
    error: str,
) -> TableProgress | None:
    """Mark a table migration as failed."""
    run = _runs.get(run_id)
    if run is None:
        return None
    tp = run.tables.get(source_table)
    if tp is None:
        return None
    tp.status = TableStatus.failed
    tp.errors.append(error)
    tp.completed_at = datetime.now(timezone.utc).isoformat()
    run.failed_tables += 1
    return tp


def skip_table(
    run_id: str,
    source_table: str,
    reason: str = "",
) -> TableProgress | None:
    """Mark a table as skipped."""
    run = _runs.get(run_id)
    if run is None:
        return None
    tp = run.tables.get(source_table)
    if tp is None:
        return None
    tp.status = TableStatus.skipped
    if reason:
        tp.errors.append(reason)
    return tp


# ═══════════════════════════════════════════════════════════════════════════════
# Cleanup
# ═══════════════════════════════════════════════════════════════════════════════


def clear_runs() -> None:
    """Remove all tracked runs (useful for testing)."""
    global _runs
    _runs = {}
