"""Migration verifier — row-count and hash-based verification.

After migration, compares source and target data to detect discrepancies:

* ``verify_counts(path, table_plan, db)`` → ``CountVerifyResult``
* ``verify_hashes(path, table_plan, db, batch_size)`` → ``HashVerifyResult``

Verification uses SHA-256 hashing of the full row content (sorted keys)
to detect even subtle differences introduced during transformation.
"""

from __future__ import annotations

import hashlib
import json as _json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from mneme.migration.dumper import dump_table, dump_row_count
from mneme.migration.loader import _transform_row
from mneme.migration.planner import TablePlan


# ═══════════════════════════════════════════════════════════════════════════════
# Data types
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class CountVerifyResult:
    """Result of row-count verification."""

    table_name: str
    source_count: int
    target_count: int
    match: bool
    difference: int = 0
    error: str | None = None


@dataclass
class HashVerifyResult:
    """Result of hash-based row verification."""

    table_name: str
    rows_checked: int
    rows_matched: int
    rows_mismatched: int
    rows_source_only: int
    rows_target_only: int
    mismatch_details: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


# ═══════════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════════


def verify_counts(
    db_path: str | Path,
    table_plan: TablePlan,
    db: Session,
) -> CountVerifyResult:
    """Compare row counts between source SQLite and target PostgreSQL."""
    try:
        source_count = dump_row_count(db_path, table_plan.source_table)
        target_count = _target_row_count(db, table_plan.target_table)

        return CountVerifyResult(
            table_name=table_plan.source_table,
            source_count=source_count,
            target_count=target_count,
            match=source_count == target_count,
            difference=target_count - source_count,
        )
    except Exception as exc:
        return CountVerifyResult(
            table_name=table_plan.source_table,
            source_count=0,
            target_count=0,
            match=False,
            error=str(exc),
        )


def verify_hashes(
    db_path: str | Path,
    table_plan: TablePlan,
    db: Session,
    *,
    batch_size: int = 500,
    pk_column: str = "id",
) -> HashVerifyResult:
    """Verify row-level data integrity via SHA-256 hash comparison.

    For each source row:
    1. Apply the same transforms the loader would apply
    2. Compute SHA-256 of the resulting dict (sorted keys, JSON-serialized)
    3. Compare with the target row (same transform + hash)

    Returns detailed mismatch information.
    """
    result = HashVerifyResult(
        table_name=table_plan.source_table,
        rows_checked=0,
        rows_matched=0,
        rows_mismatched=0,
        rows_source_only=0,
        rows_target_only=0,
    )

    try:
        source_cols = [ct.source_col for ct in table_plan.columns]

        # Build source hash set: {pk_value: hash_hex}
        source_hashes: dict[str, str] = {}
        for batch in dump_table(db_path, table_plan.source_table, source_cols, batch_size=batch_size):
            for row in batch:
                transformed = _transform_row(row, table_plan)
                pk_val = str(transformed.get(pk_column, ""))
                row_hash = _compute_row_hash(transformed)
                source_hashes[pk_val] = row_hash
                result.rows_checked += 1

        # Build target hash set (read from PG)
        target_hashes: dict[str, str] = {}
        target_cols = [ct.target_col for ct in table_plan.columns]
        _fetch_target_hashes(db, table_plan.target_table, target_cols, target_hashes, pk_column)

        # Compare
        source_pks = set(source_hashes.keys())
        target_pks = set(target_hashes.keys())

        for pk in source_pks & target_pks:
            if source_hashes[pk] == target_hashes[pk]:
                result.rows_matched += 1
            else:
                result.rows_mismatched += 1
                result.mismatch_details.append({
                    "pk": pk,
                    "source_hash": source_hashes[pk],
                    "target_hash": target_hashes[pk],
                })

        result.rows_source_only = len(source_pks - target_pks)
        result.rows_target_only = len(target_pks - source_pks)

        return result

    except Exception as exc:
        result.error = str(exc)
        return result


# ═══════════════════════════════════════════════════════════════════════════════
# Internal
# ═══════════════════════════════════════════════════════════════════════════════


def _compute_row_hash(row: dict[str, Any]) -> str:
    """Compute SHA-256 hash of a row dict (sorted keys, JSON)."""
    # Remove internal columns
    clean = {k: v for k, v in row.items() if not k.startswith("_migration")}
    row_bytes = _json.dumps(clean, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(row_bytes).hexdigest()


def _target_row_count(db: Session, table_name: str) -> int:
    """Get row count from target PostgreSQL table."""
    row = db.execute(text(f'SELECT COUNT(*) FROM "{table_name}"')).fetchone()
    return row[0] if row else 0


def _fetch_target_hashes(
    db: Session,
    table_name: str,
    columns: list[str],
    target_hashes: dict[str, str],
    pk_column: str = "id",
) -> None:
    """Fetch and hash all rows from the target table."""
    col_list = ", ".join(f'"{c}"' for c in columns)
    try:
        rows = db.execute(
            text(f'SELECT {col_list} FROM "{table_name}"')
        ).fetchall()

        for row in rows:
            row_dict = dict(row._mapping) if hasattr(row, "_mapping") else dict(zip(columns, row))
            pk_val = str(row_dict.get(pk_column, ""))
            row_hash = _compute_row_hash(row_dict)
            target_hashes[pk_val] = row_hash
    except Exception:
        pass  # Table may not exist or columns may differ
