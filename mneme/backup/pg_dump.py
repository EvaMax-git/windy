"""Standalone pg_dump / pg_restore utilities.

Provides clean, reusable functions for PostgreSQL backup and restore
operations independent of the Mneme backup engine pipeline.

Exports
-------
* ``pg_dump()`` — run pg_dump to create a database dump file
* ``pg_restore()`` — run pg_restore to restore a database from a dump
* ``parse_db_url()`` — parse a SQLAlchemy database URL into connection params
* ``check_pg_tools()`` — verify that pg_dump and pg_restore are available
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("mneme.backup.pg_dump")


# ── Database URL parsing ───────────────────────────────────────────────────────


@dataclass
class DbConnectionParams:
    """Parsed PostgreSQL connection parameters."""

    host: str = "localhost"
    port: str = "5432"
    user: str = "postgres"
    password: str = ""
    database: str = "postgres"

    def to_env(self) -> dict[str, str]:
        """Return connection parameters as PG* environment variables."""
        return {
            "PGHOST": self.host,
            "PGPORT": self.port,
            "PGUSER": self.user,
            "PGPASSWORD": self.password,
            "PGDATABASE": self.database,
        }


def parse_db_url(database_url: str) -> DbConnectionParams:
    """Parse a SQLAlchemy database URL into connection parameters.

    Supports
    --------
    * ``postgresql+psycopg2://user:pass@host:port/dbname``
    * ``postgresql://user:pass@host:port/dbname``
    * ``postgres://user:pass@host:port/dbname``

    Raises
    ------
    ValueError
        If the URL scheme is not recognised.
    """
    url = database_url

    # Strip driver prefix
    for prefix in ("postgresql+psycopg2://", "postgresql://", "postgres://"):
        if url.startswith(prefix):
            url = url[len(prefix):]
            break
    else:
        raise ValueError(f"Unsupported database URL scheme: {database_url}")

    # user:pass@host:port/dbname
    user_pass, rest = url.split("@", 1)
    host_port, dbname = rest.rsplit("/", 1)

    user = user_pass
    password = ""
    if ":" in user_pass:
        user, password = user_pass.split(":", 1)

    host = host_port
    port = "5432"
    if ":" in host_port:
        host, port = host_port.split(":", 1)

    return DbConnectionParams(
        host=host,
        port=port,
        user=user,
        password=password,
        database=dbname,
    )


# ── Tool availability check ────────────────────────────────────────────────────


def check_pg_tools() -> dict[str, bool]:
    """Check whether pg_dump and pg_restore are available on PATH.

    Returns a dict with ``pg_dump`` and ``pg_restore`` boolean keys.
    """
    result: dict[str, bool] = {}
    for tool in ("pg_dump", "pg_restore", "psql"):
        try:
            subprocess.run(
                [tool, "--version"],
                capture_output=True,
                timeout=5,
            )
            result[tool] = True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            result[tool] = False
    return result


# ── Dump format ────────────────────────────────────────────────────────────────


class DumpFormat:
    """Canonical pg_dump output format identifiers."""

    custom: str = "custom"   # -Fc  — compressed, suitable for pg_restore
    plain: str = "plain"     # -Fp  — plain SQL text
    tar: str = "tar"         # -Ft  — tar archive
    directory: str = "directory"  # -Fd — directory format


_FORMAT_FLAGS: dict[str, str] = {
    DumpFormat.custom: "c",
    DumpFormat.plain: "p",
    DumpFormat.tar: "t",
    DumpFormat.directory: "d",
}


# ── Result types ───────────────────────────────────────────────────────────────


@dataclass
class PgDumpResult:
    """Outcome of a pg_dump operation."""

    success: bool
    output_path: Path | None = None
    file_size_bytes: int = 0
    duration_seconds: float = 0.0
    error_message: str | None = None
    stderr: str = ""


@dataclass
class PgRestoreResult:
    """Outcome of a pg_restore operation."""

    success: bool
    duration_seconds: float = 0.0
    error_message: str | None = None
    stderr: str = ""


# ── pg_dump ────────────────────────────────────────────────────────────────────


def pg_dump(
    *,
    database_url: str,
    output_path: Path,
    format: str = DumpFormat.custom,
    schema_only: bool = False,
    data_only: bool = False,
    no_owner: bool = True,
    no_acl: bool = True,
    clean: bool = False,
    create: bool = False,
    include_schema: str | None = None,
    exclude_table_data: list[str] | None = None,
    extra_args: list[str] | None = None,
    timeout_seconds: int = 3600,
) -> PgDumpResult:
    """Run ``pg_dump`` and write the dump to *output_path*.

    Parameters
    ----------
    database_url:
        PostgreSQL connection URL (SQLAlchemy format).
    output_path:
        Path where the dump file will be written.
    format:
        Output format: ``"custom"``, ``"plain"``, ``"tar"``, or ``"directory"``.
        Default is ``"custom"`` (-Fc).
    schema_only:
        Dump only schema, not data.
    data_only:
        Dump only data, not schema.
    no_owner:
        Exclude ownership statements (safe for cross-system restore).
    no_acl:
        Exclude access privilege statements (GRANT/REVOKE).
    clean:
        Include DROP statements before CREATE (plain format).
    create:
        Include CREATE DATABASE statement (plain format).
    include_schema:
        Dump only the named schema.
    exclude_table_data:
        Exclude data from these tables (schema still dumped).
    extra_args:
        Additional raw arguments to pass to pg_dump.
    timeout_seconds:
        Maximum time (in seconds) to wait for pg_dump to complete.

    Returns
    -------
    PgDumpResult with success status, output path, size, and error info.
    """
    import time

    params = parse_db_url(database_url)
    env = os.environ.copy()
    env.update(params.to_env())

    fmt_flag = _FORMAT_FLAGS.get(format, "c")

    cmd = [
        "pg_dump",
        f"-F{fmt_flag}",
        "-h", params.host,
        "-p", params.port,
        "-U", params.user,
        "-d", params.database,
    ]

    if format != DumpFormat.directory:
        cmd.extend(["-f", str(output_path)])
    else:
        # Directory format: -f specifies the directory
        cmd.extend(["-f", str(output_path)])

    if schema_only:
        cmd.append("--schema-only")
    if data_only:
        cmd.append("--data-only")
    if no_owner:
        cmd.append("--no-owner")
    if no_acl:
        cmd.append("--no-acl")
    if clean:
        cmd.append("--clean")
    if create:
        cmd.append("--create")
    if include_schema:
        cmd.extend(["--schema", include_schema])
    if exclude_table_data:
        for table in exclude_table_data:
            cmd.extend(["--exclude-table-data", table])
    if extra_args:
        cmd.extend(extra_args)

    logger.info("pg_dump: %s", " ".join(cmd))
    started = time.monotonic()

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout_seconds,
        )
        elapsed = round(time.monotonic() - started, 2)

        if result.returncode != 0:
            return PgDumpResult(
                success=False,
                output_path=output_path,
                duration_seconds=elapsed,
                error_message=f"pg_dump exited with code {result.returncode}",
                stderr=result.stderr,
            )

        file_size = output_path.stat().st_size if output_path.exists() else 0

        logger.info(
            "pg_dump succeeded: %s (%,d bytes, %.1fs)",
            output_path, file_size, elapsed,
        )

        return PgDumpResult(
            success=True,
            output_path=output_path,
            file_size_bytes=file_size,
            duration_seconds=elapsed,
            stderr=result.stderr,
        )

    except subprocess.TimeoutExpired:
        elapsed = round(time.monotonic() - started, 2)
        return PgDumpResult(
            success=False,
            output_path=output_path,
            duration_seconds=elapsed,
            error_message=f"pg_dump timed out after {timeout_seconds}s",
        )
    except FileNotFoundError:
        elapsed = round(time.monotonic() - started, 2)
        return PgDumpResult(
            success=False,
            output_path=output_path,
            duration_seconds=elapsed,
            error_message="pg_dump not found on PATH. Install PostgreSQL client tools.",
        )
    except Exception as exc:
        elapsed = round(time.monotonic() - started, 2)
        return PgDumpResult(
            success=False,
            output_path=output_path,
            duration_seconds=elapsed,
            error_message=str(exc),
        )


# ── pg_restore ─────────────────────────────────────────────────────────────────


def pg_restore(
    *,
    database_url: str,
    input_path: Path,
    format: str = DumpFormat.custom,
    clean: bool = True,
    create: bool = False,
    single_transaction: bool = True,
    no_owner: bool = True,
    no_acl: bool = True,
    schema_only: bool = False,
    data_only: bool = False,
    include_schema: str | None = None,
    include_table: list[str] | None = None,
    extra_args: list[str] | None = None,
    timeout_seconds: int = 3600,
) -> PgRestoreResult:
    """Restore a database from a pg_dump file using ``pg_restore``.

    Parameters
    ----------
    database_url:
        Target PostgreSQL connection URL (SQLAlchemy format).
    input_path:
        Path to the dump file to restore from.
    format:
        Input format: ``"custom"``, ``"plain"``, ``"tar"``, or ``"directory"``.
        Default is ``"custom"`` (-Fc).
    clean:
        Drop database objects before recreating them.
    create:
        Include CREATE DATABASE statement (requires clean).
    single_transaction:
        Wrap the restore in a single transaction (custom format only).
        Rolls back everything on error.
    no_owner:
        Skip restoration of object ownership.
    no_acl:
        Skip restoration of access privileges.
    schema_only:
        Restore only schema, not data.
    data_only:
        Restore only data, not schema.
    include_schema:
        Restore only objects in the named schema.
    include_table:
        Restore only the named tables.
    extra_args:
        Additional raw arguments to pass to pg_restore.
    timeout_seconds:
        Maximum time (in seconds) to wait for pg_restore to complete.

    Returns
    -------
    PgRestoreResult with success status and error info.
    """
    import time

    params = parse_db_url(database_url)
    env = os.environ.copy()
    env.update(params.to_env())

    fmt_flag = _FORMAT_FLAGS.get(format, "c")

    cmd = [
        "pg_restore",
        f"-F{fmt_flag}",
        "-h", params.host,
        "-p", params.port,
        "-U", params.user,
        "-d", params.database,
    ]

    if clean:
        cmd.append("--clean")
    if create:
        cmd.append("--create")
    if single_transaction and format != DumpFormat.plain:
        cmd.append("--single-transaction")
    if no_owner:
        cmd.append("--no-owner")
    if no_acl:
        cmd.append("--no-acl")
    if schema_only:
        cmd.append("--schema-only")
    if data_only:
        cmd.append("--data-only")
    if include_schema:
        cmd.extend(["--schema", include_schema])
    if include_table:
        for table in include_table:
            cmd.extend(["--table", table])
    if extra_args:
        cmd.extend(extra_args)

    # Input file comes last
    cmd.append(str(input_path))

    logger.info("pg_restore: %s", " ".join(cmd))
    started = time.monotonic()

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout_seconds,
        )
        elapsed = round(time.monotonic() - started, 2)

        if result.returncode != 0:
            return PgRestoreResult(
                success=False,
                duration_seconds=elapsed,
                error_message=f"pg_restore exited with code {result.returncode}",
                stderr=result.stderr,
            )

        logger.info("pg_restore succeeded: %s (%.1fs)", input_path, elapsed)

        return PgRestoreResult(
            success=True,
            duration_seconds=elapsed,
            stderr=result.stderr,
        )

    except subprocess.TimeoutExpired:
        elapsed = round(time.monotonic() - started, 2)
        return PgRestoreResult(
            success=False,
            duration_seconds=elapsed,
            error_message=f"pg_restore timed out after {timeout_seconds}s",
        )
    except FileNotFoundError:
        elapsed = round(time.monotonic() - started, 2)
        return PgRestoreResult(
            success=False,
            duration_seconds=elapsed,
            error_message="pg_restore not found on PATH. Install PostgreSQL client tools.",
        )
    except Exception as exc:
        elapsed = round(time.monotonic() - started, 2)
        return PgRestoreResult(
            success=False,
            duration_seconds=elapsed,
            error_message=str(exc),
        )
