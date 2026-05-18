"""P2-14 Backup engine — execute pg_dump, collect row counts, generate manifest.

The backup engine:
1. Determines output directory (``MnemeData/backups/YYYY-MM-DDTHHMMSS/``)
2. Runs ``pg_dump -Fc`` to create a custom-format dump (via :mod:`mneme.backup.pg_dump`)
3. Queries row counts for all 45 tables
4. Looks up the current Alembic revision
5. Computes SHA-256 checksum of the dump
6. Writes ``manifest.json`` alongside the dump
7. Optionally creates a ``jobs`` record to track execution
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from mneme.backup.manifest import (
    MNAME_TABLES,
    BackupManifest,
    compute_sha256,
    save_manifest,
    validate_manifest,
    verify_checksum as _verify_checksum,
)
from mneme.backup.pg_dump import (
    DbConnectionParams,
    DumpFormat,
    parse_db_url,
    pg_dump,
)
from mneme.config import get_settings

logger = logging.getLogger("mneme.backup")


# ── Default backup root ──────────────────────────────────────────────────────


def _default_backup_root() -> Path:
    """Return the default backup root directory.

    Uses ``MNEME_BACKUP_ROOT`` from the environment/settings or falls back to
    ``MnemeData/backups`` relative to the current working directory.
    """
    try:
        settings = get_settings()
        root = getattr(settings, "backup_root", None)
        if root:
            return Path(root)
    except Exception:
        pass
    return Path.cwd() / "MnemeData" / "backups"


# ── PostgreSQL helpers ────────────────────────────────────────────────────────


def _extract_db_params(database_url: str) -> DbConnectionParams:
    """Parse a SQLAlchemy database URL into connection parameters.

    Delegates to :func:`mneme.backup.pg_dump.parse_db_url`.
    """
    return parse_db_url(database_url)


def _get_pg_version(db_params: DbConnectionParams) -> str:
    """Query the PostgreSQL server version string."""
    env = os.environ.copy()
    env.update(db_params.to_env())
    try:
        result = subprocess.run(
            ["psql", "-t", "-A", "-c", "SELECT version();"],
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )
        if result.returncode == 0:
            version_line = result.stdout.strip()
            # Extract version like "16.4" from "PostgreSQL 16.4 ..."
            import re
            match = re.search(r"PostgreSQL ([\d.]+)", version_line)
            if match:
                return match.group(1)
            return version_line[:50]
        return "unknown"
    except Exception as exc:
        logger.warning("Failed to detect PostgreSQL version: %s", exc)
        return "unknown"


def _get_alembic_revision(db_params: DbConnectionParams) -> str:
    """Query the current Alembic revision from the database."""
    env = os.environ.copy()
    env.update(db_params.to_env())
    try:
        result = subprocess.run(
            ["psql", "-t", "-A", "-c", "SELECT version_num FROM alembic_version LIMIT 1;"],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return "unknown"
    except Exception as exc:
        logger.warning("Failed to detect Alembic revision: %s", exc)
        return "unknown"


def _get_table_row_counts(db_params: DbConnectionParams) -> dict[str, int]:
    """Query row counts for all 45 Mneme tables.

    Returns a dictionary mapping table name → row count.
    """
    env = os.environ.copy()
    env.update(db_params.to_env())

    # Build a query that SELECTs count(*) for each table
    queries = "\nUNION ALL\n".join(
        f"SELECT '{t}' AS tbl, COALESCE((SELECT count(*) FROM {t}), 0) AS cnt"
        for t in MNAME_TABLES
    )
    full_query = f"SELECT tbl, cnt FROM ({queries}) sub ORDER BY tbl;"

    try:
        result = subprocess.run(
            ["psql", "-t", "-A", "-F", "|", "-c", full_query],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        if result.returncode != 0:
            logger.error("Row count query failed: %s", result.stderr)
            return {}

        counts: dict[str, int] = {}
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            parts = line.split("|")
            if len(parts) >= 2:
                try:
                    counts[parts[0].strip()] = int(parts[1].strip())
                except (ValueError, IndexError):
                    continue
        return counts
    except Exception as exc:
        logger.warning("Failed to query row counts: %s", exc)
        return {}


# ── Backup execution ──────────────────────────────────────────────────────────


class BackupResult:
    """Outcome of a backup operation."""

    __slots__ = ("success", "manifest", "output_dir", "error_message")

    def __init__(
        self,
        success: bool,
        manifest: BackupManifest | None = None,
        output_dir: Path | None = None,
        error_message: str | None = None,
    ) -> None:
        self.success = success
        self.manifest = manifest
        self.output_dir = output_dir
        self.error_message = error_message


def run_backup(
    *,
    database_url: str | None = None,
    output_root: Path | None = None,
    backup_id: str | None = None,
) -> BackupResult:
    """Execute a complete backup: pg_dump → manifest → checksum.

    Parameters
    ----------
    database_url:
        PostgreSQL connection URL. If None, read from application settings.
    output_root:
        Root directory for backups. If None, use the default.
    backup_id:
        UUID string for the backup. If None, a random UUID is generated.

    Returns
    -------
    ``BackupResult`` with success status, manifest, and output directory.
    """
    if database_url is None:
        try:
            database_url = get_settings().database_url
        except Exception:
            raise ValueError("database_url is required when settings are not available")

    if output_root is None:
        output_root = _default_backup_root()

    if backup_id is None:
        backup_id = str(uuid4())

    # Parse DB params
    db_params = _extract_db_params(database_url)
    logger.info("Backup started: backup_id=%s, host=%s, db=%s",
                backup_id, db_params.host, db_params.database)

    # Create timestamped output directory
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%S")
    output_dir = output_root / timestamp
    output_dir.mkdir(parents=True, exist_ok=True)

    dump_path = output_dir / "backup.dump"
    started_at = datetime.now(timezone.utc)

    try:
        # 1. Run pg_dump
        _run_pg_dump(db_params, dump_path)

        # 2. Compute file size
        file_size = dump_path.stat().st_size

        # 3. Compute SHA-256 checksum
        checksum = compute_sha256(dump_path)
        logger.info("Backup dump created: %s (%,d bytes, sha256=%s)",
                     dump_path, file_size, checksum[:16])

        # 4. Get PostgreSQL version
        pg_version = _get_pg_version(db_params)

        # 5. Get Alembic revision
        alembic_revision = _get_alembic_revision(db_params)

        # 6. Get table row counts
        table_row_counts = _get_table_row_counts(db_params)
        logger.info("Row counts collected for %d tables", len(table_row_counts))

        # 7. Build manifest
        completed_at = datetime.now(timezone.utc).isoformat()
        manifest = BackupManifest(
            backup_id=backup_id,
            created_at=started_at.isoformat(),
            pg_version=pg_version,
            format="custom",
            tables=45,
            table_row_counts=table_row_counts,
            file_path=str(dump_path),
            file_size_bytes=file_size,
            checksum_sha256=checksum,
            alembic_revision=alembic_revision,
            status="succeeded",
            completed_at=completed_at,
            dump_command=f"pg_dump -Fc -h {db_params.host} -p {db_params.port} "
                         f"-U {db_params.user} -d {db_params.database}",
            env_info={
                "python_version": sys.version.split()[0],
                "platform": sys.platform,
            },
        )

        # 8. Validate manifest
        issues = validate_manifest(manifest)
        if issues:
            logger.warning("Manifest validation issues: %s", issues)
            # Don't fail the backup for non-critical manifest issues

        # 9. Save manifest
        save_manifest(manifest, output_dir)
        logger.info("Backup complete: backup_id=%s, output=%s", backup_id, output_dir)

        return BackupResult(success=True, manifest=manifest, output_dir=output_dir)

    except subprocess.CalledProcessError as exc:
        error_msg = f"pg_dump failed (exit code {exc.returncode}): {exc.stderr}"
        logger.error(error_msg)
        # Write failure manifest
        manifest = BackupManifest(
            backup_id=backup_id,
            created_at=started_at.isoformat(),
            pg_version=_safe_pg_version(db_params),
            status="failed",
            error_message=error_msg,
            completed_at=datetime.now(timezone.utc).isoformat(),
            alembic_revision="unknown",
        )
        save_manifest(manifest, output_dir)
        return BackupResult(success=False, manifest=manifest, output_dir=output_dir, error_message=error_msg)

    except Exception as exc:
        error_msg = f"Backup failed: {exc}"
        logger.exception(error_msg)
        manifest = BackupManifest(
            backup_id=backup_id,
            created_at=started_at.isoformat(),
            pg_version=_safe_pg_version(db_params),
            status="failed",
            error_message=error_msg,
            completed_at=datetime.now(timezone.utc).isoformat(),
            alembic_revision="unknown",
        )
        save_manifest(manifest, output_dir)
        return BackupResult(success=False, manifest=manifest, output_dir=output_dir, error_message=error_msg)


def _safe_pg_version(db_params: DbConnectionParams) -> str:
    """Try to get PG version, return 'unknown' on failure."""
    try:
        return _get_pg_version(db_params)
    except Exception:
        return "unknown"


def _run_pg_dump(db_params: DbConnectionParams, output_path: Path) -> None:
    """Execute ``pg_dump -Fc`` to *output_path* via :func:`mneme.backup.pg_dump.pg_dump`.

    Raises ``subprocess.CalledProcessError`` on failure.
    """
    # Reconstruct database URL for the pg_dump module
    db_url = (
        f"postgresql://{db_params.user}"
        + (f":{db_params.password}" if db_params.password else "")
        + f"@{db_params.host}:{db_params.port}/{db_params.database}"
    )

    result = pg_dump(
        database_url=db_url,
        output_path=output_path,
        format=DumpFormat.custom,
        no_owner=True,
        no_acl=True,
    )

    if not result.success:
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=["pg_dump", "-Fc", f"-f {output_path}"],
            output="",
            stderr=result.error_message or result.stderr,
        )


# ── Integrity verification ────────────────────────────────────────────────────


def verify_backup(manifest: BackupManifest) -> dict[str, Any]:
    """Verify a backup's integrity using the manifest.

    Checks:
    1. Dump file exists and size matches
    2. SHA-256 checksum matches
    3. Manifest has all expected fields

    Returns a dict with ``valid`` boolean and ``issues`` list.
    """
    issues: list[str] = []

    # Validate manifest fields
    issues.extend(validate_manifest(manifest))

    # Check dump file
    dump_path = Path(manifest.file_path)
    if not dump_path.exists():
        issues.append(f"Dump file not found: {dump_path}")
        return {"valid": False, "issues": issues}

    # Check file size
    actual_size = dump_path.stat().st_size
    if actual_size != manifest.file_size_bytes:
        issues.append(
            f"File size mismatch: manifest={manifest.file_size_bytes}, actual={actual_size}"
        )

    # Check checksum
    if manifest.checksum_sha256:
        if not _verify_checksum(manifest, dump_path):
            actual = compute_sha256(dump_path)
            issues.append(
                f"Checksum mismatch: manifest={manifest.checksum_sha256[:16]}..., "
                f"actual={actual[:16]}..."
            )
    else:
        issues.append("No checksum in manifest")

    return {
        "valid": len(issues) == 0,
        "issues": issues,
    }


# ── Convenience: list available backups ───────────────────────────────────────


def list_backups(output_root: Path | None = None) -> list[dict[str, Any]]:
    """List all backups found under *output_root*, newest first.

    Each entry is a summarized dict suitable for API responses.
    """
    from mneme.backup.manifest import find_all_manifests

    if output_root is None:
        output_root = _default_backup_root()

    results: list[dict[str, Any]] = []
    for directory, manifest in find_all_manifests(output_root):
        results.append({
            "backup_id": manifest.backup_id,
            "created_at": manifest.created_at,
            "pg_version": manifest.pg_version,
            "status": manifest.status,
            "file_size_bytes": manifest.file_size_bytes,
            "alembic_revision": manifest.alembic_revision,
            "tables": manifest.tables,
            "table_count_summary": _summarize_row_counts(manifest.table_row_counts),
            "checksum_sha256": manifest.checksum_sha256[:16] + "..." if manifest.checksum_sha256 else "",
            "backup_directory": str(directory),
        })
    return results


def _summarize_row_counts(counts: dict[str, int]) -> dict[str, int]:
    """Return a brief summary: total rows and non-empty tables."""
    total = sum(counts.values())
    non_empty = sum(1 for c in counts.values() if c > 0)
    return {"total_rows": total, "non_empty_tables": non_empty}
