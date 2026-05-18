"""Migration planner — builds the migration plan from source schema + manifest.

Takes the discovered source schema and the migration manifest, then produces
a ``MigrationPlan`` that defines *what* gets migrated, *in what order*, and
*how* each column is transformed.

Design
------
* ``build_plan(schema)`` → ``MigrationPlan`` (ordered table plans)
* Each ``TablePlan`` contains the target table name, column transformations,
  and whether the table should be migrated or skipped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mneme.migration.discovery import SourceSchema, TableInfo
from mneme.migration.manifest import (
    COLUMN_MAP,
    DEFAULT_BATCH_SIZE,
    ENUM_MAP,
    MIGRATION_ORDER,
    NEW_COLUMN_DEFAULTS,
    TABLE_MAP,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Data types
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class ColumnTransform:
    """Describes how a single column is transformed during migration."""

    source_col: str
    target_col: str
    transform: str | None  # None="copy", "skip", "uuid", "int", "datetime", "enum:X", "default:V", "json"
    enum_domain: str | None = None
    default_value: Any = None


@dataclass
class TablePlan:
    """Migration plan for a single table."""

    source_table: str
    target_table: str
    source_row_count: int
    columns: list[ColumnTransform] = field(default_factory=list)
    pk_columns: list[str] = field(default_factory=list)
    skip: bool = False
    skip_reason: str = ""
    new_column_defaults: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass
class MigrationPlan:
    """Complete migration plan for a source database."""

    source_path: str
    sqlite_version: str
    tables: list[TablePlan] = field(default_factory=list)
    total_source_rows: int = 0
    total_skipped_tables: int = 0
    batch_size: int = DEFAULT_BATCH_SIZE


# ═══════════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════════


def build_plan(schema: SourceSchema, *, batch_size: int = DEFAULT_BATCH_SIZE) -> MigrationPlan:
    """Build a complete migration plan from the discovered source schema.

    Tables are ordered by ``MIGRATION_ORDER`` (dependency-first). Tables in
    the source but not in ``TABLE_MAP`` are automatically skipped with a
    warning. Tables in ``TABLE_MAP`` with a ``None`` target are explicitly
    skipped.
    """
    plan = MigrationPlan(
        source_path=schema.path,
        sqlite_version=schema.sqlite_version,
        batch_size=batch_size,
    )

    # Build a set of known legacy table names for fast lookup
    known_legacy = set(TABLE_MAP.keys())
    source_tables = set(schema.tables.keys())

    # Step 1: process tables in dependency order
    for legacy_name in MIGRATION_ORDER:
        if legacy_name not in source_tables:
            continue  # table not present in source

        table_info = schema.tables[legacy_name]
        table_plan = _plan_table(table_info)
        plan.tables.append(table_plan)

    # Step 2: handle unknown source tables (not in TABLE_MAP or MIGRATION_ORDER)
    for legacy_name in sorted(source_tables - known_legacy):
        table_info = schema.tables[legacy_name]
        plan.tables.append(TablePlan(
            source_table=legacy_name,
            target_table="",
            source_row_count=table_info.row_count,
            skip=True,
            skip_reason=f"Unknown table — not in TABLE_MAP. Add to manifest if migration is desired.",
            warnings=[f"Table '{legacy_name}' has no mapping defined"],
        ))

    # Step 3: aggregate totals
    plan.total_source_rows = sum(t.source_row_count for t in plan.tables if not t.skip)
    plan.total_skipped_tables = sum(1 for t in plan.tables if t.skip)

    return plan


def build_table_plan(table_info: TableInfo) -> TablePlan:
    """Build a migration plan for a single table (standalone use)."""
    return _plan_table(table_info)


# ═══════════════════════════════════════════════════════════════════════════════
# Internal
# ═══════════════════════════════════════════════════════════════════════════════


def _plan_table(table_info: TableInfo) -> TablePlan:
    """Produce a TablePlan for one source table."""
    legacy_name = table_info.name

    # Check TABLE_MAP
    target_name = TABLE_MAP.get(legacy_name)
    if target_name is None:
        return TablePlan(
            source_table=legacy_name,
            target_table="",
            source_row_count=table_info.row_count,
            skip=True,
            skip_reason=(
                f"Table '{legacy_name}' mapped to None in TABLE_MAP — explicitly skipped"
                if legacy_name in TABLE_MAP
                else f"Table '{legacy_name}' not found in TABLE_MAP"
            ),
            pk_columns=list(table_info.pk_columns),
        )

    # Build column transforms
    col_map = COLUMN_MAP.get(legacy_name, {})
    transforms: list[ColumnTransform] = []
    warnings: list[str] = []

    for col_info in table_info.columns:
        if col_info.name in col_map:
            target_col, transform_spec = col_map[col_info.name]
            if transform_spec == "skip":
                continue  # Skip this column entirely

            transform = _parse_transform(transform_spec, transforms, target_col, legacy_name)
            transforms.append(transform)
        else:
            # Auto-map: same name, no transform
            transforms.append(ColumnTransform(
                source_col=col_info.name,
                target_col=col_info.name,
                transform=None,
            ))

            # Only warn about unmapped non-pk columns
            if not col_info.is_pk:
                warnings.append(
                    f"Column '{legacy_name}.{col_info.name}' auto-mapped "
                    f"(no entry in COLUMN_MAP). Verify correctness."
                )

    # Get new column defaults for v4 columns not in source
    new_defaults = NEW_COLUMN_DEFAULTS.get(legacy_name, {})

    return TablePlan(
        source_table=legacy_name,
        target_table=target_name,
        source_row_count=table_info.row_count,
        columns=transforms,
        pk_columns=list(table_info.pk_columns),
        new_column_defaults=new_defaults,
        warnings=warnings,
    )


def _parse_transform(
    spec: str | None,
    transforms: list[ColumnTransform],
    target_col: str,
    legacy_name: str,
) -> ColumnTransform:
    """Parse a transform specification string into a ColumnTransform."""
    if spec is None:
        return ColumnTransform(
            source_col=target_col,
            target_col=target_col,
            transform=None,
        )

    if spec.startswith("enum:"):
        enum_domain = spec[len("enum:"):]
        return ColumnTransform(
            source_col=target_col,
            target_col=target_col,
            transform="enum",
            enum_domain=enum_domain,
        )

    if spec.startswith("default:"):
        import json as _json
        raw_val = spec[len("default:"):]
        try:
            default_val = _json.loads(raw_val)
        except Exception:
            default_val = raw_val
        return ColumnTransform(
            source_col=target_col,
            target_col=target_col,
            transform="default",
            default_value=default_val,
        )

    return ColumnTransform(
        source_col=target_col,
        target_col=target_col,
        transform=spec,
    )
