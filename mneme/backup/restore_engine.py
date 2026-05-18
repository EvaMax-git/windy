"""P2-15 Restore engine — pg_restore, verification, and drill report generation.

The restore engine:
1. Creates a temporary database for drill-mode restore
2. Executes ``pg_restore --clean --if-exists`` from a backup dump
3. Runs post-restore verification: table count, row counts, FK integrity, Alembic stamp
4. Generates a structured restore report (JSON)
5. Cleans up the temporary database after a drill

Modes
-----
* **drill**: restore to a new temporary database, verify, report, then clean up
* **live**: restore directly to a target database (requires explicit confirmation,
  and in production a ``review_item`` of type ``restore_confirm`` must be created
  — that integration is handled in P2-16)
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from mneme.backup.engine import (
    _default_backup_root,
    _get_alembic_revision,
    _get_table_row_counts,
)
from mneme.backup.manifest import (
    BackupManifest,
    load_manifest,
    verify_checksum,
)
from mneme.backup.pg_dump import (
    DbConnectionParams,
    parse_db_url,
)

logger = logging.getLogger("mneme.backup.restore")


# ─────────────────────────────────────────────────────────────────────────────────
# Restore report data class
# ─────────────────────────────────────────────────────────────────────────────────


class RestoreReport:
    """Structured restore drill / live report."""

    __slots__ = (
        "restore_id",
        "backup_id",
        "restore_type",
        "started_at",
        "completed_at",
        "status",
        "target_database",
        "source_backup",
        "verification",
        "error_message",
    )

    def __init__(
        self,
        *,
        restore_id: str,
        backup_id: str,
        restore_type: str,
        started_at: str,
        completed_at: str | None = None,
        status: str = "in_progress",
        target_database: str = "",
        source_backup: dict[str, Any] | None = None,
        verification: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> None:
        self.restore_id = restore_id
        self.backup_id = backup_id
        self.restore_type = restore_type
        self.started_at = started_at
        self.completed_at = completed_at or ""
        self.status = status
        self.target_database = target_database
        self.source_backup = source_backup or {}
        self.verification = verification or _empty_verification()
        self.error_message = error_message

    def to_dict(self) -> dict[str, Any]:
        return {
            "restore_id": self.restore_id,
            "backup_id": self.backup_id,
            "restore_type": self.restore_type,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "status": self.status,
            "target_database": self.target_database,
            "source_backup": self.source_backup,
            "verification": self.verification,
            "error_message": self.error_message,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RestoreReport:
        return cls(
            restore_id=data.get("restore_id", ""),
            backup_id=data.get("backup_id", ""),
            restore_type=data.get("restore_type", "drill"),
            started_at=data.get("started_at", ""),
            completed_at=data.get("completed_at"),
            status=data.get("status", "in_progress"),
            target_database=data.get("target_database", ""),
            source_backup=data.get("source_backup", {}),
            verification=data.get("verification", _empty_verification()),
            error_message=data.get("error_message"),
        )


def _empty_verification() -> dict[str, Any]:
    return {
        "table_count": {"expected": 45, "actual": 0, "match": False},
        "row_counts": {
            "match": False,
            "mismatches": [],
            "manifest_tables_not_in_restored": [],
            "restored_tables_not_in_manifest": [],
        },
        "foreign_keys": {"valid": False, "violations": []},
        "alembic_revision": {"expected": "", "actual": "", "match": False},
    }


# ─────────────────────────────────────────────────────────────────────────────────
# DB helpers for restore
# ─────────────────────────────────────────────────────────────────────────────────


def _build_admin_db_params(database_url: str) -> dict[str, str]:
    """Build pg-env dict for connecting to the postgres maintenance database."""
    db_params = parse_db_url(database_url)
    admin = db_params.to_env()
    admin["PGDATABASE"] = "postgres"
    return admin


def _create_database(db_name: str, admin_db_params: dict[str, str]) -> None:
    """Create a new PostgreSQL database named *db_name*."""
    env = os.environ.copy()
    env.update(admin_db_params)

    logger.info("Creating temporary database '%s'", db_name)
    result = subprocess.run(
        ["createdb", db_name],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to create database '{db_name}': {result.stderr.strip()}"
        )
    logger.info("Database '%s' created successfully", db_name)


def _drop_database(db_name: str, admin_db_params: dict[str, str]) -> None:
    """Drop a PostgreSQL database named *db_name*."""
    env = os.environ.copy()
    env.update(admin_db_params)

    logger.info("Dropping temporary database '%s'", db_name)
    result = subprocess.run(
        ["dropdb", "--if-exists", db_name],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    if result.returncode != 0:
        logger.warning(
            "Failed to drop database '%s': %s", db_name, result.stderr.strip()
        )
    else:
        logger.info("Database '%s' dropped", db_name)


# ─────────────────────────────────────────────────────────────────────────────────
# pg_restore execution
# ─────────────────────────────────────────────────────────────────────────────────


def _run_pg_restore(
    db_params: DbConnectionParams,
    dump_path: Path,
    *,
    clean: bool = True,
) -> None:
    """Execute ``pg_restore`` to restore a custom-format dump.

    Raises ``subprocess.CalledProcessError`` on failure.
    """
    env = os.environ.copy()
    env.update(db_params.to_env())

    cmd = [
        "pg_restore",
        "-h", db_params.host,
        "-p", db_params.port,
        "-U", db_params.user,
        "-d", db_params.database,
        "--no-owner",
        "--no-acl",
    ]

    if clean:
        cmd.extend(["--clean", "--if-exists"])

    cmd.append(str(dump_path))

    logger.info("Running: %s", " ".join(cmd))

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        timeout=3600,
    )

    if result.returncode != 0:
        logger.error("pg_restore stderr: %s", result.stderr)
        logger.error("pg_restore stdout: %s", result.stdout)
        raise subprocess.CalledProcessError(
            result.returncode, cmd,
            output=result.stdout,
            stderr=result.stderr,
        )

    if result.stderr.strip():
        logger.info("pg_restore warnings: %s", result.stderr.strip())


# ─────────────────────────────────────────────────────────────────────────────────
# Verification queries
# ─────────────────────────────────────────────────────────────────────────────────


def _run_sql_query(
    db_params: dict[str, str], query: str, timeout: int = 30
) -> tuple[bool, str]:
    """Run a SQL query via psql and return (success, stdout)."""
    env = os.environ.copy()
    env.update(db_params)
    try:
        result = subprocess.run(
            ["psql", "-t", "-A", "-c", query],
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
        )
        if result.returncode != 0:
            logger.warning("SQL query failed: %s", result.stderr.strip())
            return False, result.stderr.strip()
        return True, result.stdout.strip()
    except Exception as exc:
        return False, str(exc)


def _verify_table_count(
    db_params: dict[str, str], expected: int = 45
) -> dict[str, Any]:
    """Verify the number of user tables in the restored database."""
    query = (
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE';"
    )
    ok, stdout = _run_sql_query(db_params, query)
    if not ok:
        return {"expected": expected, "actual": 0, "match": False, "error": stdout}
    try:
        actual = int(stdout.strip())
    except ValueError:
        return {
            "expected": expected,
            "actual": 0,
            "match": False,
            "error": f"Unparseable: {stdout}",
        }
    return {"expected": expected, "actual": actual, "match": actual == expected}


def _verify_table_names(db_params: dict[str, str]) -> list[str]:
    """Return the list of public table names in the restored database."""
    query = (
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
        "ORDER BY table_name;"
    )
    ok, stdout = _run_sql_query(db_params, query, timeout=30)
    if not ok:
        return []
    return [line.strip() for line in stdout.split("\n") if line.strip()]


def _verify_row_counts(
    db_params: dict[str, str],
    expected_counts: dict[str, int],
) -> dict[str, Any]:
    """Compare row counts between restored DB and backup manifest."""
    actual_counts = _get_table_row_counts(db_params)
    manifest_tables = set(expected_counts.keys())
    restored_tables = set(actual_counts.keys())

    manifest_not_restored = sorted(manifest_tables - restored_tables)
    restored_not_manifest = sorted(restored_tables - manifest_tables)

    mismatches: list[dict[str, Any]] = []
    for tbl in sorted(manifest_tables & restored_tables):
        expected = expected_counts[tbl]
        actual = actual_counts[tbl]
        if expected != actual:
            mismatches.append({
                "table": tbl,
                "expected": expected,
                "actual": actual,
            })

    match = (
        len(mismatches) == 0
        and len(manifest_not_restored) == 0
        and len(restored_not_manifest) == 0
    )

    return {
        "match": match,
        "mismatches": mismatches,
        "manifest_tables_not_in_restored": manifest_not_restored,
        "restored_tables_not_in_manifest": restored_not_manifest,
    }


def _verify_foreign_keys(db_params: dict[str, str]) -> dict[str, Any]:
    """Check for broken foreign key references in the restored DB."""
    fk_query = """
    SELECT
        tc.table_name AS child_table,
        kcu.column_name AS child_column,
        ccu.table_name AS parent_table,
        ccu.column_name AS parent_column,
        tc.constraint_name
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu
        ON tc.constraint_name = kcu.constraint_name
        AND tc.table_schema = kcu.table_schema
    JOIN information_schema.constraint_column_usage ccu
        ON ccu.constraint_name = tc.constraint_name
        AND ccu.table_schema = tc.table_schema
    WHERE tc.constraint_type = 'FOREIGN KEY'
      AND tc.table_schema = 'public'
    ORDER BY tc.table_name, tc.constraint_name;
    """

    ok, stdout = _run_sql_query(db_params, fk_query, timeout=30)
    if not ok:
        return {"valid": False, "violations": [{"error": f"FK query failed: {stdout}"}]}

    violations: list[dict[str, Any]] = []

    for line in stdout.split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) < 5:
            continue
        child_table = parts[0].strip()
        child_col = parts[1].strip()
        parent_table = parts[2].strip()
        parent_col = parts[3].strip()

        check_query = (
            f"SELECT count(*) FROM {child_table} c "
            f"WHERE c.{child_col} IS NOT NULL "
            f"AND NOT EXISTS (SELECT 1 FROM {parent_table} p WHERE p.{parent_col} = c.{child_col});"
        )
        ck_ok, ck_stdout = _run_sql_query(db_params, check_query, timeout=30)
        if not ck_ok:
            violations.append({
                "child_table": child_table,
                "child_column": child_col,
                "parent_table": parent_table,
                "parent_column": parent_col,
                "error": f"Check query failed: {ck_stdout}",
            })
            continue
        try:
            orphan_count = int(ck_stdout.strip())
        except ValueError:
            orphan_count = -1

        if orphan_count > 0:
            violations.append({
                "child_table": child_table,
                "child_column": child_col,
                "parent_table": parent_table,
                "parent_column": parent_col,
                "orphan_count": orphan_count,
            })
        elif orphan_count < 0:
            violations.append({
                "child_table": child_table,
                "child_column": child_col,
                "parent_table": parent_table,
                "parent_column": parent_col,
                "error": "Could not parse orphan count",
            })

    return {"valid": len(violations) == 0, "violations": violations}


def _verify_alembic_revision(
    db_params: dict[str, str], expected: str
) -> dict[str, Any]:
    """Verify the Alembic revision stamp in the restored database."""
    actual = _get_alembic_revision(db_params)
    return {
        "expected": expected,
        "actual": actual,
        "match": actual == expected,
    }


def _run_full_verification(
    db_params: dict[str, str],
    manifest: BackupManifest,
) -> dict[str, Any]:
    """Run all verification checks against the restored database."""
    table_count = _verify_table_count(db_params, expected=manifest.tables)
    row_counts = _verify_row_counts(db_params, manifest.table_row_counts)
    foreign_keys = _verify_foreign_keys(db_params)
    alembic = _verify_alembic_revision(db_params, manifest.alembic_revision)

    return {
        "table_count": table_count,
        "row_counts": row_counts,
        "foreign_keys": foreign_keys,
        "alembic_revision": alembic,
    }


# ─────────────────────────────────────────────────────────────────────────────────
# Restore result
# ─────────────────────────────────────────────────────────────────────────────────


class RestoreResult:
    """Outcome of a restore operation."""

    __slots__ = ("success", "report", "output_dir", "error_message")

    def __init__(
        self,
        success: bool,
        report: RestoreReport | None = None,
        output_dir: Path | None = None,
        error_message: str | None = None,
    ) -> None:
        self.success = success
        self.report = report
        self.output_dir = output_dir
        self.error_message = error_message


# ─────────────────────────────────────────────────────────────────────────────────
# Restore execution — drill mode
# ─────────────────────────────────────────────────────────────────────────────────


def run_restore_drill(
    *,
    backup_id: str,
    source_database_url: str | None = None,
    target_database_url: str | None = None,
    output_root: Path | None = None,
    keep_temp_db: bool = False,
) -> RestoreResult:
    """Execute a full restore drill: restore → verify → report → clean up.

    Creates a temporary database, restores into it, runs verification,
    writes a report, and drops the temp database (unless ``keep_temp_db=True``).
    """
    from mneme.config import get_settings

    if source_database_url is None:
        try:
            source_database_url = get_settings().database_url
        except Exception:
            raise ValueError(
                "source_database_url is required when settings are not available"
            )

    if output_root is None:
        output_root = _default_backup_root()

    # 1. Locate the backup manifest
    manifest, backup_dir = _find_backup(backup_id, output_root)
    if manifest is None:
        msg = f"Backup '{backup_id}' not found under {output_root}"
        logger.error(msg)
        return RestoreResult(success=False, error_message=msg)

    if manifest.status != "succeeded":
        msg = f"Backup '{backup_id}' has status '{manifest.status}', refusing to restore"
        logger.error(msg)
        return RestoreResult(success=False, error_message=msg)

    dump_path = Path(manifest.file_path)
    if not dump_path.exists():
        msg = f"Dump file not found: {dump_path}"
        logger.error(msg)
        return RestoreResult(success=False, error_message=msg)

    # 2. Verify dump integrity before attempting restore
    if manifest.checksum_sha256:
        if not verify_checksum(manifest, dump_path):
            msg = f"Checksum mismatch for backup '{backup_id}'"
            logger.error(msg)
            return RestoreResult(success=False, error_message=msg)

    # 3. Prepare report directory
    restore_id = str(uuid4())
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%S")
    report_dir = output_root / f"restore-{timestamp}"
    report_dir.mkdir(parents=True, exist_ok=True)

    started_at = datetime.now(timezone.utc).isoformat()

    source_backup_info = {
        "backup_id": manifest.backup_id,
        "created_at": manifest.created_at,
        "file_path": manifest.file_path,
        "file_size_bytes": manifest.file_size_bytes,
        "checksum_sha256": (
            manifest.checksum_sha256[:16] + "..."
        ) if manifest.checksum_sha256 else "",
    }

    # 4. Determine target database
    source_params = parse_db_url(source_database_url)
    admin_params = _build_admin_db_params(source_database_url)

    if target_database_url is not None:
        target_params = parse_db_url(target_database_url)
        target_db_name = target_params.database
    else:
        short_ts = timestamp.replace("-", "").replace("T", "").replace(":", "")
        target_db_name = f"mneme_restore_drill_{short_ts}"
        target_params = DbConnectionParams(
            host=source_params.host,
            port=source_params.port,
            user=source_params.user,
            password=source_params.password,
            database=target_db_name,
        )

    # 5. Create temp database
    try:
        _create_database(target_db_name, admin_params)
    except Exception as exc:
        msg = f"Failed to create temp database '{target_db_name}': {exc}"
        logger.error(msg)
        report = RestoreReport(
            restore_id=restore_id,
            backup_id=backup_id,
            restore_type="drill",
            started_at=started_at,
            completed_at=datetime.now(timezone.utc).isoformat(),
            status="failed",
            target_database=target_db_name,
            source_backup=source_backup_info,
            error_message=msg,
        )
        _save_report(report, report_dir)
        return RestoreResult(
            success=False, report=report, output_dir=report_dir, error_message=msg
        )

    # 6. Execute pg_restore
    try:
        _run_pg_restore(target_params, dump_path, clean=True)
        logger.info("pg_restore completed successfully to '%s'", target_db_name)
    except subprocess.CalledProcessError as exc:
        msg = f"pg_restore failed: {exc.stderr.strip() if exc.stderr else str(exc)}"
        logger.error(msg)
        if not keep_temp_db:
            try:
                _drop_database(target_db_name, admin_params)
            except Exception:
                pass
        report = RestoreReport(
            restore_id=restore_id,
            backup_id=backup_id,
            restore_type="drill",
            started_at=started_at,
            completed_at=datetime.now(timezone.utc).isoformat(),
            status="failed",
            target_database=target_db_name,
            source_backup=source_backup_info,
            error_message=msg,
        )
        _save_report(report, report_dir)
        return RestoreResult(
            success=False, report=report, output_dir=report_dir, error_message=msg
        )
    except Exception as exc:
        msg = f"Restore failed: {exc}"
        logger.exception(msg)
        if not keep_temp_db:
            try:
                _drop_database(target_db_name, admin_params)
            except Exception:
                pass
        report = RestoreReport(
            restore_id=restore_id,
            backup_id=backup_id,
            restore_type="drill",
            started_at=started_at,
            completed_at=datetime.now(timezone.utc).isoformat(),
            status="failed",
            target_database=target_db_name,
            source_backup=source_backup_info,
            error_message=msg,
        )
        _save_report(report, report_dir)
        return RestoreResult(
            success=False, report=report, output_dir=report_dir, error_message=msg
        )

    # 7. Run verification
    verification = _run_full_verification(target_params, manifest)

    # 8. Determine overall success
    all_passed = (
        verification["table_count"].get("match", False)
        and verification["row_counts"].get("match", False)
        and verification["foreign_keys"].get("valid", False)
        and verification["alembic_revision"].get("match", False)
    )

    completed_at = datetime.now(timezone.utc).isoformat()

    report = RestoreReport(
        restore_id=restore_id,
        backup_id=backup_id,
        restore_type="drill",
        started_at=started_at,
        completed_at=completed_at,
        status="succeeded" if all_passed else "failed",
        target_database=target_db_name,
        source_backup=source_backup_info,
        verification=verification,
        error_message=None if all_passed else "One or more verification checks failed",
    )

    # 9. Save report
    _save_report(report, report_dir)

    # 10. Clean up temp database
    if not keep_temp_db:
        try:
            _drop_database(target_db_name, admin_params)
            logger.info("Temporary database '%s' cleaned up", target_db_name)
        except Exception as exc:
            logger.warning(
                "Failed to clean up temp database '%s': %s", target_db_name, exc
            )

    return RestoreResult(
        success=all_passed,
        report=report,
        output_dir=report_dir,
        error_message=report.error_message,
    )


def run_restore_live(
    *,
    backup_id: str,
    target_database_url: str,
    output_root: Path | None = None,
) -> RestoreResult:
    """Execute a **live** restore directly to a target database.

    .. warning::
       This will overwrite data in the target database. In production this must
       be gated behind a ``review_item`` of type ``restore_confirm`` (P2-16).
    """
    from mneme.config import get_settings

    if output_root is None:
        output_root = _default_backup_root()

    # 1. Locate backup manifest
    manifest, backup_dir = _find_backup(backup_id, output_root)
    if manifest is None:
        msg = f"Backup '{backup_id}' not found"
        logger.error(msg)
        return RestoreResult(success=False, error_message=msg)

    if manifest.status != "succeeded":
        msg = f"Backup '{backup_id}' has status '{manifest.status}'"
        logger.error(msg)
        return RestoreResult(success=False, error_message=msg)

    dump_path = Path(manifest.file_path)
    if not dump_path.exists():
        msg = f"Dump file not found: {dump_path}"
        logger.error(msg)
        return RestoreResult(success=False, error_message=msg)

    if manifest.checksum_sha256:
        if not verify_checksum(manifest, dump_path):
            msg = f"Checksum mismatch for backup '{backup_id}'"
            logger.error(msg)
            return RestoreResult(success=False, error_message=msg)

    restore_id = str(uuid4())
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%S")
    report_dir = output_root / f"restore-{timestamp}"
    report_dir.mkdir(parents=True, exist_ok=True)

    started_at = datetime.now(timezone.utc).isoformat()

    source_backup_info = {
        "backup_id": manifest.backup_id,
        "created_at": manifest.created_at,
        "file_path": manifest.file_path,
        "file_size_bytes": manifest.file_size_bytes,
        "checksum_sha256": (
            manifest.checksum_sha256[:16] + "..."
        ) if manifest.checksum_sha256 else "",
    }

    target_params = parse_db_url(target_database_url)
    target_db_name = target_params.database

    try:
        _run_pg_restore(target_params, dump_path, clean=True)
    except subprocess.CalledProcessError as exc:
        msg = f"pg_restore failed: {exc.stderr.strip() if exc.stderr else str(exc)}"
        logger.error(msg)
        report = RestoreReport(
            restore_id=restore_id,
            backup_id=backup_id,
            restore_type="live",
            started_at=started_at,
            completed_at=datetime.now(timezone.utc).isoformat(),
            status="failed",
            target_database=target_db_name,
            source_backup=source_backup_info,
            error_message=msg,
        )
        _save_report(report, report_dir)
        return RestoreResult(
            success=False, report=report, output_dir=report_dir, error_message=msg
        )
    except Exception as exc:
        msg = f"Restore failed: {exc}"
        logger.exception(msg)
        report = RestoreReport(
            restore_id=restore_id,
            backup_id=backup_id,
            restore_type="live",
            started_at=started_at,
            completed_at=datetime.now(timezone.utc).isoformat(),
            status="failed",
            target_database=target_db_name,
            source_backup=source_backup_info,
            error_message=msg,
        )
        _save_report(report, report_dir)
        return RestoreResult(
            success=False, report=report, output_dir=report_dir, error_message=msg
        )

    # Verification
    verification = _run_full_verification(target_params, manifest)

    all_passed = (
        verification["table_count"].get("match", False)
        and verification["row_counts"].get("match", False)
        and verification["foreign_keys"].get("valid", False)
        and verification["alembic_revision"].get("match", False)
    )

    completed_at = datetime.now(timezone.utc).isoformat()
    report = RestoreReport(
        restore_id=restore_id,
        backup_id=backup_id,
        restore_type="live",
        started_at=started_at,
        completed_at=completed_at,
        status="succeeded" if all_passed else "failed",
        target_database=target_db_name,
        source_backup=source_backup_info,
        verification=verification,
        error_message=None if all_passed else "One or more verification checks failed",
    )

    _save_report(report, report_dir)
    return RestoreResult(
        success=all_passed,
        report=report,
        output_dir=report_dir,
        error_message=report.error_message,
    )


# ─────────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────────


def _find_backup(
    backup_id: str, backup_root: Path
) -> tuple[BackupManifest | None, Path | None]:
    """Find a backup by ID under *backup_root*."""
    for entry in sorted(backup_root.iterdir(), reverse=True):
        if entry.is_dir():
            manifest = load_manifest(entry)
            if manifest is not None and manifest.backup_id == backup_id:
                return manifest, entry
    return None, None


def _save_report(report: RestoreReport, directory: Path) -> Path:
    """Write the restore report JSON to *directory*."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "restore_report.json"
    path.write_text(report.to_json(), encoding="utf-8")
    logger.info("Restore report saved to %s", path)
    return path


def load_restore_report(directory: Path) -> RestoreReport | None:
    """Load a ``restore_report.json`` from *directory*."""
    path = directory / "restore_report.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return RestoreReport.from_dict(data)
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def find_all_restore_reports(
    backups_root: Path,
) -> list[tuple[Path, RestoreReport]]:
    """Scan *backups_root* for ``restore_report.json`` files, newest first."""
    results: list[tuple[Path, RestoreReport]] = []
    if not backups_root.exists():
        return results

    for entry in sorted(backups_root.iterdir(), reverse=True):
        if entry.is_dir():
            report = load_restore_report(entry)
            if report is not None:
                results.append((entry, report))
    return results


def list_restores(
    output_root: Path | None = None,
) -> list[dict[str, Any]]:
    """List all restore reports, newest first."""
    if output_root is None:
        output_root = _default_backup_root()

    results: list[dict[str, Any]] = []
    for directory, report in find_all_restore_reports(output_root):
        results.append({
            "restore_id": report.restore_id,
            "backup_id": report.backup_id,
            "restore_type": report.restore_type,
            "status": report.status,
            "started_at": report.started_at,
            "completed_at": report.completed_at,
            "target_database": report.target_database,
            "report_directory": str(directory),
        })
    return results
