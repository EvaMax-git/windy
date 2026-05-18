"""Source database discovery — scan SQLite schema, tables, and column metadata.

Connects to a legacy SQLite database file, enumerates all user tables and
their column definitions, and exposes row-count estimates to feed the
planner and dumper.

Design
------
* ``discover_schema(path)`` → ``SourceSchema`` with tables + columns
* ``discover_table_info(path, table)`` → ``TableInfo`` with row count, PK
* All connections are read-only (``mode=ro``, ``uri=True`` for WAL safety)
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ═══════════════════════════════════════════════════════════════════════════════
# Data types
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class ColumnInfo:
    """Metadata for a single column in a source table."""

    name: str
    data_type: str
    nullable: bool
    default_value: Any | None = None
    is_pk: bool = False


@dataclass
class TableInfo:
    """Metadata for a single source table."""

    name: str
    columns: list[ColumnInfo] = field(default_factory=list)
    row_count: int = 0
    pk_columns: list[str] = field(default_factory=list)


@dataclass
class SourceSchema:
    """Complete snapshot of the source database schema."""

    path: str
    tables: dict[str, TableInfo] = field(default_factory=dict)
    sqlite_version: str = ""


# ═══════════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════════


def discover_schema(db_path: str | Path) -> SourceSchema:
    """Connect to a SQLite file and discover its full schema.

    Returns a ``SourceSchema`` containing every user table, its columns,
    and approximate row counts.

    Args:
        db_path: Filesystem path to the SQLite database file.

    Returns:
        Fully populated ``SourceSchema``.
    """
    db_path = str(db_path)

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    try:
        schema = SourceSchema(path=db_path)
        schema.sqlite_version = _get_sqlite_version(conn)

        table_names = _list_user_tables(conn)
        for tname in table_names:
            table_info = _discover_table(conn, tname)
            schema.tables[tname] = table_info

        return schema
    finally:
        conn.close()


def discover_table_info(db_path: str | Path, table_name: str) -> TableInfo | None:
    """Discover metadata for a single table.

    Returns ``None`` if the table does not exist.
    """
    db_path = str(db_path)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        if not exists:
            return None
        return _discover_table(conn, table_name)
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _get_sqlite_version(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT sqlite_version()").fetchone()
    return row[0] if row else "unknown"


def _list_user_tables(conn: sqlite3.Connection) -> list[str]:
    """Return sorted list of user table names, excluding sqlite_* internal tables."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    ).fetchall()
    return [r["name"] for r in rows]


def _discover_table(conn: sqlite3.Connection, table_name: str) -> TableInfo:
    """Build a TableInfo for a single table."""
    # Column info via PRAGMA
    pragma_rows = conn.execute(f"PRAGMA table_info('{table_name}')").fetchall()

    columns: list[ColumnInfo] = []
    pk_columns: list[str] = []
    for row in pragma_rows:
        col = ColumnInfo(
            name=row["name"],
            data_type=row["type"] or "",
            nullable=not bool(row["notnull"]),
            default_value=row["dflt_value"],
            is_pk=bool(row["pk"]),
        )
        columns.append(col)
        if col.is_pk:
            pk_columns.append(col.name)

    # Row count — fast estimate via sqlite_stat1 or exact count for small tables
    row_count = _estimate_row_count(conn, table_name)

    return TableInfo(
        name=table_name,
        columns=columns,
        row_count=row_count,
        pk_columns=pk_columns,
    )


def _estimate_row_count(conn: sqlite3.Connection, table_name: str) -> int:
    """Return exact row count (fast enough for migration-scale tables)."""
    try:
        row = conn.execute(
            f'SELECT COUNT(*) AS cnt FROM "{table_name}"'
        ).fetchone()
        return row["cnt"] if row else 0
    except sqlite3.Error:
        return 0
