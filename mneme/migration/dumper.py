"""Source data dumper — stream rows from the legacy SQLite database.

Provides a generator-based API that yields rows in configurable batches,
keeping memory usage low even for large tables.

Design
------
* ``dump_table(path, table, columns, batch_size)`` → Iterator[list[dict]]
* ``dump_row_count(path, table)`` → int
* Uses ``sqlite3.Row`` → ``dict`` conversion for each row.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any


# ═══════════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════════


def dump_table(
    db_path: str | Path,
    table_name: str,
    columns: list[str],
    *,
    batch_size: int = 500,
) -> Iterator[list[dict[str, Any]]]:
    """Yield rows from a SQLite table in batches.

    Each batch is a ``list[dict[str, Any]]`` where keys are column names
    and values are raw SQLite types. The caller is responsible for
    applying field transformations.

    Args:
        db_path: Path to the SQLite database file.
        table_name: Name of the table to read.
        columns: List of column names to select.
        batch_size: Maximum number of rows per batch.

    Yields:
        Batches of row dicts.
    """
    db_path = str(db_path)
    col_list = ", ".join(f'"{c}"' for c in columns)

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.execute(f'SELECT {col_list} FROM "{table_name}"')
        batch: list[dict[str, Any]] = []
        for row in cursor:
            batch.append(dict(row))
            if len(batch) >= batch_size:
                yield batch
                batch = []
        if batch:
            yield batch
    finally:
        conn.close()


def dump_table_with_offset(
    db_path: str | Path,
    table_name: str,
    columns: list[str],
    *,
    offset: int = 0,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Read a single page of rows (for legacy API window)."""
    db_path = str(db_path)
    col_list = ", ".join(f'"{c}"' for c in columns)

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f'SELECT {col_list} FROM "{table_name}" LIMIT ? OFFSET ?',
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def dump_single_row(
    db_path: str | Path,
    table_name: str,
    pk_columns: list[str],
    pk_values: list[Any],
    columns: list[str],
) -> dict[str, Any] | None:
    """Read a single row by primary key."""
    db_path = str(db_path)
    col_list = ", ".join(f'"{c}"' for c in columns)
    where = " AND ".join(f'"{c}" = ?' for c in pk_columns)

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            f'SELECT {col_list} FROM "{table_name}" WHERE {where}',
            pk_values,
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def dump_row_count(db_path: str | Path, table_name: str) -> int:
    """Return the exact row count for a table."""
    db_path = str(db_path)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()
        return row[0] if row else 0
    finally:
        conn.close()
