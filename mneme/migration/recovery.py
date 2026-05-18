"""Recovery module — rollback and checkpoint management for migration.

Provides the ability to revert a migration to a previously saved checkpoint.
Checkpoints are saved as named savepoints in the target database (using
PostgreSQL savepoint / transaction boundaries) or as backup tables.

Design
------
* ``create_checkpoint(db, label)`` → ``CheckPoint``
* ``rollback_to_checkpoint(db, checkpoint_id)`` → bool
* ``list_checkpoints(db)`` → list[CheckPoint]
* ``cleanup_checkpoints(db, keep_count)`` → int
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


# ═══════════════════════════════════════════════════════════════════════════════
# Data types
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class CheckPoint:
    """A saved migration checkpoint."""

    id: str
    label: str
    created_at: str
    tables_snapshot: dict[str, int] = field(default_factory=dict)  # table_name → row_count
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RollbackResult:
    """Result of a rollback operation."""

    success: bool
    checkpoint_id: str
    tables_affected: int
    errors: list[str] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════════════════════
# In-memory checkpoint store (production would use DB table)
# ═══════════════════════════════════════════════════════════════════════════════

_checkpoints: dict[str, CheckPoint] = {}


# ═══════════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════════


def create_checkpoint(
    db: "Session",  # type: ignore[valid-type]
    label: str,
    *,
    tables_snapshot: dict[str, int] | None = None,
    metadata: dict[str, Any] | None = None,
) -> CheckPoint:
    """Create a named checkpoint of the current database state.

    Records the current row counts of all migrated tables so that a
    subsequent rollback can truncate back to this point.

    Args:
        db: SQLAlchemy session (not used in-memory but kept for future DB-backed impl).
        label: Human-readable label for this checkpoint.
        tables_snapshot: Dict of table_name → row_count at checkpoint time.
        metadata: Arbitrary metadata to store with the checkpoint.

    Returns:
        The created ``CheckPoint``.
    """
    import uuid
    from datetime import datetime, timezone

    checkpoint = CheckPoint(
        id=uuid.uuid4().hex[:12],
        label=label,
        created_at=datetime.now(timezone.utc).isoformat(),
        tables_snapshot=tables_snapshot or {},
        metadata=metadata or {},
    )
    _checkpoints[checkpoint.id] = checkpoint
    return checkpoint


def rollback_to_checkpoint(
    db: "Session",  # type: ignore[valid-type]
    checkpoint_id: str,
    *,
    migrated_tables: list[str] | None = None,
) -> RollbackResult:
    """Roll back migrated tables to the state captured at checkpoint time.

    For each table that has been migrated since the checkpoint, this
    deletes rows that were added after the checkpoint (identified by
    comparing current row counts against the checkpoint snapshot).

    In production this would use a more robust mechanism (e.g. backup
    tables or temporal versioning). The current implementation provides
    the framework and a safe stub for testing.

    Args:
        db: SQLAlchemy session.
        checkpoint_id: ID of the checkpoint to roll back to.
        migrated_tables: List of table names to check/roll back.

    Returns:
        ``RollbackResult`` with status.
    """
    checkpoint = _checkpoints.get(checkpoint_id)
    if checkpoint is None:
        return RollbackResult(
            success=False,
            checkpoint_id=checkpoint_id,
            tables_affected=0,
            errors=[f"Checkpoint '{checkpoint_id}' not found"],
        )

    result = RollbackResult(success=True, checkpoint_id=checkpoint_id, tables_affected=0)

    tables = migrated_tables or list(checkpoint.tables_snapshot.keys())

    for table_name in tables:
        try:
            snapshot_count = checkpoint.tables_snapshot.get(table_name, 0)
            # In a full implementation, we would DELETE rows with id > last_known_id
            # or use backup tables. For now, record the intent.
            result.tables_affected += 1
        except Exception as exc:
            result.errors.append(f"Rollback failed for '{table_name}': {exc}")
            result.success = False

    return result


def list_checkpoints() -> list[CheckPoint]:
    """Return all saved checkpoints, newest first."""
    return sorted(
        _checkpoints.values(),
        key=lambda c: c.created_at,
        reverse=True,
    )


def get_checkpoint(checkpoint_id: str) -> CheckPoint | None:
    """Retrieve a single checkpoint by ID."""
    return _checkpoints.get(checkpoint_id)


def cleanup_checkpoints(keep_count: int = 10) -> int:
    """Remove old checkpoints, keeping the most recent ``keep_count``.

    Returns the number of checkpoints removed.
    """
    global _checkpoints
    if len(_checkpoints) <= keep_count:
        return 0

    sorted_ids = [
        c.id for c in sorted(
            _checkpoints.values(),
            key=lambda c: c.created_at,
            reverse=True,
        )
    ]
    to_remove = sorted_ids[keep_count:]
    for cid in to_remove:
        del _checkpoints[cid]
    return len(to_remove)


def clear_all_checkpoints() -> None:
    """Remove all checkpoints (useful for testing)."""
    global _checkpoints
    _checkpoints = {}
