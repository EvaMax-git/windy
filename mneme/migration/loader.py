"""Target data loader — batch UPSERT rows into PostgreSQL.

Applies column transformations (type casting, enum mapping, default values)
and writes batches to the target PostgreSQL database using SQLAlchemy Core
for maximum throughput.

Design
------
* ``load_batch(table_plan, rows, db, *, dry_run)`` → ``LoadResult``
* ``create_shadow_table(table_plan, db)`` → str (temp table name)
* Dry-run mode prints the generated SQL without executing it.
"""

from __future__ import annotations

import hashlib
import json as _json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from mneme.migration.manifest import ENUM_MAP
from mneme.migration.planner import ColumnTransform, TablePlan


# ═══════════════════════════════════════════════════════════════════════════════
# Data types
# ═══════════════════════════════════════════════════════════════════════════════


class LoadResult:
    """Outcome of loading a single batch."""

    __slots__ = ("rows_loaded", "rows_skipped", "errors", "sql_preview")

    def __init__(self, rows_loaded: int = 0, rows_skipped: int = 0) -> None:
        self.rows_loaded = rows_loaded
        self.rows_skipped = rows_skipped
        self.errors: list[str] = []
        self.sql_preview: list[str] = []


# ═══════════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════════


def load_batch(
    db: Session,
    table_plan: TablePlan,
    rows: list[dict[str, Any]],
    *,
    dry_run: bool = False,
) -> LoadResult:
    """Transform and load a batch of source rows into the target table.

    Args:
        db: SQLAlchemy session for the target PostgreSQL database.
        table_plan: Migration plan for this table.
        rows: Raw row dicts from the dumper.
        dry_run: If True, only generate SQL preview strings; do not execute.

    Returns:
        ``LoadResult`` with counts and any errors.
    """
    if not rows:
        return LoadResult()

    target_table = table_plan.target_table
    result = LoadResult()

    transformed_rows: list[dict[str, Any]] = []
    for source_row in rows:
        try:
            transformed = _transform_row(source_row, table_plan)
            transformed_rows.append(transformed)
        except Exception as exc:
            result.errors.append(f"Transform error: {exc}")
            result.rows_skipped += 1

    if not transformed_rows:
        return result

    if dry_run:
        result.sql_preview = _generate_dry_run_sql(target_table, transformed_rows, table_plan)
        result.rows_loaded = len(transformed_rows)
        return result

    # Execute UPSERT
    try:
        _execute_upsert(db, target_table, transformed_rows, table_plan)
        result.rows_loaded = len(transformed_rows)
    except Exception as exc:
        result.errors.append(f"Insert error: {exc}")
        result.rows_skipped += len(transformed_rows)

    return result


def create_shadow_table(db: Session, table_plan: TablePlan) -> str:
    """Create a shadow (temporary) table for safe migration testing.

    The shadow table mirrors the target table structure and is prefixed
    with ``_migration_shadow_``.

    Returns:
        The shadow table name.
    """
    shadow_name = f"_migration_shadow_{table_plan.target_table}"

    # Drop if exists
    db.execute(text(f'DROP TABLE IF EXISTS "{shadow_name}" CASCADE'))
    db.execute(text(
        f'CREATE TABLE "{shadow_name}" (LIKE "{table_plan.target_table}" INCLUDING ALL)'
    ))
    db.commit()
    return shadow_name


def drop_shadow_table(db: Session, shadow_name: str) -> None:
    """Drop a shadow table."""
    db.execute(text(f'DROP TABLE IF EXISTS "{shadow_name}" CASCADE'))
    db.commit()


# ═══════════════════════════════════════════════════════════════════════════════
# Internal — row transformation
# ═══════════════════════════════════════════════════════════════════════════════


def _transform_row(
    source_row: dict[str, Any],
    table_plan: TablePlan,
) -> dict[str, Any]:
    """Apply column transforms and new-column defaults to a single source row."""
    output: dict[str, Any] = {}

    for ct in table_plan.columns:
        value = source_row.get(ct.source_col)
        output[ct.target_col] = _apply_transform(value, ct)

    # Add new-column defaults
    for col_name, default_val in table_plan.new_column_defaults.items():
        output[col_name] = default_val

    # Compute row hash for verification
    row_bytes = _json.dumps(output, sort_keys=True, default=str).encode("utf-8")
    output["_migration_hash"] = hashlib.sha256(row_bytes).hexdigest()

    return output


def _apply_transform(value: Any, ct: ColumnTransform) -> Any:
    """Apply a single column transform to a value."""
    transform = ct.transform

    if transform is None:
        return value

    if value is None:
        return None

    if transform == "uuid":
        if isinstance(value, UUID):
            return value
        return UUID(str(value))

    if transform == "int":
        return int(value)

    if transform == "float":
        return float(value)

    if transform == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return bool(value)
        if isinstance(value, str):
            return value.lower() in ("1", "true", "yes", "on")
        return bool(value)

    if transform == "json":
        if isinstance(value, str):
            try:
                return _json.loads(value)
            except (_json.JSONDecodeError, TypeError):
                return value
        return value

    if transform == "datetime":
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            # Try ISO format
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                return value
        return value

    if transform == "enum":
        domain = ct.enum_domain
        if domain and domain in ENUM_MAP:
            mapped = ENUM_MAP[domain].get(str(value))
            if mapped is not None:
                return mapped
        # Fall though: return original
        return value

    if transform == "default":
        return ct.default_value

    # Unknown transform — pass through
    return value


# ═══════════════════════════════════════════════════════════════════════════════
# Internal — SQL execution
# ═══════════════════════════════════════════════════════════════════════════════


def _execute_upsert(
    db: Session,
    target_table: str,
    rows: list[dict[str, Any]],
    table_plan: TablePlan,
) -> None:
    """Execute a batch UPSERT using PostgreSQL ON CONFLICT."""
    if not rows:
        return

    # Remove internal columns (_migration_hash)
    clean_rows: list[dict[str, Any]] = []
    internal_cols = {"_migration_hash"}
    for row in rows:
        clean_rows.append({k: v for k, v in row.items() if k not in internal_cols})

    # Build UPSERT
    columns = list(clean_rows[0].keys())
    col_names = ", ".join(f'"{c}"' for c in columns)
    placeholders = ", ".join(f":{c}" for c in columns)

    # ON CONFLICT: use PK columns from plan (or id fallback)
    pk_cols = table_plan.pk_columns if table_plan.pk_columns else ["id"]
    conflict_target = ", ".join(f'"{c}"' for c in pk_cols)

    # Build update SET clause — update all non-PK columns
    non_pk_cols = [c for c in columns if c not in pk_cols]
    if non_pk_cols:
        update_set = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in non_pk_cols)
        sql = (
            f'INSERT INTO "{target_table}" ({col_names}) '
            f"VALUES ({placeholders}) "
            f"ON CONFLICT ({conflict_target}) "
            f"DO UPDATE SET {update_set}"
        )
    else:
        sql = (
            f'INSERT INTO "{target_table}" ({col_names}) '
            f"VALUES ({placeholders}) "
            f"ON CONFLICT ({conflict_target}) DO NOTHING"
        )

    for row in clean_rows:
        db.execute(text(sql), row)

    db.commit()


def _generate_dry_run_sql(
    target_table: str,
    rows: list[dict[str, Any]],
    table_plan: TablePlan,
) -> list[str]:
    """Generate preview SQL for dry-run mode."""
    preview: list[str] = []
    internal_cols = {"_migration_hash"}

    pk_cols = table_plan.pk_columns if table_plan.pk_columns else ["id"]
    conflict_target = ", ".join(f'"{c}"' for c in pk_cols)

    for row in rows[:5]:  # Show first 5 only to avoid flooding
        clean = {k: v for k, v in row.items() if k not in internal_cols}
        values = ", ".join(_format_sql_value(v) for v in clean.values())
        col_names = ", ".join(f'"{c}"' for c in clean.keys())
        sql = (
            f'INSERT INTO "{target_table}" ({col_names}) '
            f"VALUES ({values}) "
            f"ON CONFLICT ({conflict_target}) DO UPDATE ..."
        )
        preview.append(sql)

    if len(rows) > 5:
        preview.append(f"... and {len(rows) - 5} more rows")

    return preview


def _format_sql_value(value: Any) -> str:
    """Format a Python value for SQL display."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (str, UUID)):
        escaped = str(value).replace("'", "''")
        return f"'{escaped}'"
    return f"'{str(value)}'"
