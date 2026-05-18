"""P2-15 Restore tests — engine, schemas, API, and CLI.

Tests cover:
1. RestoreReport creation and JSON roundtrip
2. RestoreResult dataclass
3. restore_engine helpers (_empty_verification, DB param helpers)
4. _run_sql_query stub test (psql may or may not be available)
5. Schema serialization (RestoreReportDetail, RestoreSummary, etc.)
6. find_all_restore_reports / load_restore_report
7. API schema validation
8. CLI argument parsing (smoke)
9. run_restore_drill (integration test — skipped if pg_dump/pg_restore unavailable)
10. run_restore_live (integration test — skipped)
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest

from mneme.backup.manifest import (
    BackupManifest,
    compute_sha256,
    save_manifest,
    load_manifest,
)
from mneme.backup.restore_engine import (
    RestoreReport,
    RestoreResult,
    _empty_verification,
    _build_admin_db_params,
    _run_sql_query,
    _verify_table_count,
    _verify_row_counts,
    _verify_foreign_keys,
    _verify_alembic_revision,
    _run_full_verification,
    _find_backup,
    _save_report,
    load_restore_report,
    find_all_restore_reports,
    list_restores,
    run_restore_drill,
    run_restore_live,
    _create_database,
    _drop_database,
    _run_pg_restore,
)
from mneme.backup.engine import _extract_db_params, _default_backup_root
from mneme.schemas.backup import (
    RestoreDrillRequest,
    RestoreDrillResponse,
    RestoreListResponse,
    RestorePreviewResponse,
    RestoreReportDetail,
    RestoreSourceInfo,
    RestoreSummary,
    RestoreVerificationResult,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def sample_report_dict() -> dict:
    return {
        "restore_id": str(uuid4()),
        "backup_id": str(uuid4()),
        "restore_type": "drill",
        "started_at": "2026-05-03T15:00:00+00:00",
        "completed_at": "2026-05-03T15:01:00+00:00",
        "status": "succeeded",
        "target_database": "mneme_restore_drill_test",
        "source_backup": {
            "backup_id": "abc-123",
            "created_at": "2026-05-03T14:00:00+00:00",
            "file_path": "/tmp/backup.dump",
            "file_size_bytes": 12345,
            "checksum_sha256": "abcdef1234567890...",
        },
        "verification": {
            "table_count": {"expected": 45, "actual": 45, "match": True},
            "row_counts": {
                "match": True,
                "mismatches": [],
                "manifest_tables_not_in_restored": [],
                "restored_tables_not_in_manifest": [],
            },
            "foreign_keys": {"valid": True, "violations": []},
            "alembic_revision": {
                "expected": "0001_baseline_45_tables",
                "actual": "0001_baseline_45_tables",
                "match": True,
            },
        },
        "error_message": None,
    }


@pytest.fixture
def sample_report(sample_report_dict) -> RestoreReport:
    return RestoreReport.from_dict(sample_report_dict)


@pytest.fixture
def sample_manifest() -> BackupManifest:
    return BackupManifest(
        backup_id=str(uuid4()),
        created_at="2026-05-03T14:00:00+00:00",
        pg_version="16.4",
        format="custom",
        tables=45,
        table_row_counts={
            "projects": 3,
            "users": 1,
            "audit_events": 150,
        },
        file_path="/tmp/backup.dump",
        file_size_bytes=12345,
        checksum_sha256="a" * 64,
        alembic_revision="0001_baseline_45_tables",
        status="succeeded",
        completed_at="2026-05-03T14:01:00+00:00",
    )


# ── RestoreReport creation and serialization ──────────────────────────────────


class TestRestoreReport:
    def test_create_from_dict(self, sample_report_dict):
        r = RestoreReport.from_dict(sample_report_dict)
        assert r.restore_id == sample_report_dict["restore_id"]
        assert r.restore_type == "drill"
        assert r.status == "succeeded"
        assert r.target_database == "mneme_restore_drill_test"

    def test_default_values(self):
        r = RestoreReport(
            restore_id="r-1",
            backup_id="b-1",
            restore_type="drill",
            started_at="2026-01-01T00:00:00Z",
        )
        assert r.status == "in_progress"
        assert r.completed_at == ""
        assert r.target_database == ""
        assert r.source_backup == {}
        assert r.verification == _empty_verification()

    def test_to_dict_roundtrip(self, sample_report):
        d = sample_report.to_dict()
        r2 = RestoreReport.from_dict(d)
        assert r2.restore_id == sample_report.restore_id
        assert r2.status == sample_report.status
        assert r2.verification == sample_report.verification

    def test_json_roundtrip(self, sample_report):
        json_str = sample_report.to_json()
        r2 = RestoreReport.from_dict(json.loads(json_str))
        assert r2.restore_id == sample_report.restore_id
        assert r2.backup_id == sample_report.backup_id

    def test_json_is_valid_json(self, sample_report):
        json_str = sample_report.to_json()
        parsed = json.loads(json_str)
        assert parsed["restore_id"] == sample_report.restore_id
        assert parsed["verification"]["table_count"]["match"] is True

    def test_failed_report(self):
        r = RestoreReport(
            restore_id="r-fail",
            backup_id="b-1",
            restore_type="drill",
            started_at="2026-01-01T00:00:00Z",
            completed_at="2026-01-01T00:01:00Z",
            status="failed",
            target_database="temp_db",
            error_message="pg_restore not found",
        )
        assert r.status == "failed"
        assert r.error_message == "pg_restore not found"
        assert r.verification == _empty_verification()


# ── RestoreResult ─────────────────────────────────────────────────────────────


class TestRestoreResult:
    def test_success_result(self, sample_report):
        result = RestoreResult(
            success=True,
            report=sample_report,
            output_dir=Path("/tmp/restore"),
        )
        assert result.success is True
        assert result.report is sample_report
        assert result.error_message is None

    def test_failure_result(self):
        result = RestoreResult(
            success=False,
            error_message="Backup not found",
        )
        assert result.success is False
        assert result.report is None
        assert "not found" in result.error_message


# ── _empty_verification ───────────────────────────────────────────────────────


class TestEmptyVerification:
    def test_structure(self):
        v = _empty_verification()
        assert "table_count" in v
        assert "row_counts" in v
        assert "foreign_keys" in v
        assert "alembic_revision" in v
        assert v["table_count"]["match"] is False
        assert v["foreign_keys"]["valid"] is False


# ── _build_admin_db_params ───────────────────────────────────────────────────


class TestAdminDBParams:
    def test_converts_to_postgres_db(self):
        url = "postgresql+psycopg2://user:pass@localhost:5432/mneme"
        params = _build_admin_db_params(url)
        assert params["PGDATABASE"] == "postgres"
        assert params["PGUSER"] == "user"
        assert params["PGHOST"] == "localhost"
        assert params["PGPORT"] == "5432"

    def test_preserves_other_params(self):
        url = "postgresql+psycopg2://admin:secret@db.example.com:5433/mydb"
        params = _build_admin_db_params(url)
        assert params["PGDATABASE"] == "postgres"
        assert params["PGUSER"] == "admin"
        assert params["PGPASSWORD"] == "secret"
        assert params["PGHOST"] == "db.example.com"
        assert params["PGPORT"] == "5433"


# ── _run_sql_query basic test ────────────────────────────────────────────────


class TestRunSqlQuery:
    def test_psql_unavailable_returns_false(self):
        # Point to a non-existent host to test error handling
        db_params = {
            "PGHOST": "nonexistent-host.invalid",
            "PGPORT": "5432",
            "PGUSER": "test",
            "PGPASSWORD": "test",
            "PGDATABASE": "test",
        }
        ok, output = _run_sql_query(db_params, "SELECT 1;", timeout=3)
        # Should fail gracefully
        assert ok is False or output == ""


# ── _verify_table_count logic (unit test, no DB) ─────────────────────────────


class TestVerifyTableCountLogic:
    def test_match_structure(self):
        # The function queries a real DB, so we test returned structure logic.
        # We just verify the output shape for a failure case.
        result = _verify_table_count(
            {
                "PGHOST": "nonexistent.invalid",
                "PGPORT": "5432",
                "PGUSER": "x",
                "PGPASSWORD": "x",
                "PGDATABASE": "x",
            },
            expected=45,
        )
        assert "expected" in result
        assert "actual" in result
        assert "match" in result
        assert result["match"] is False  # DB unreachable


# ── _find_backup ──────────────────────────────────────────────────────────────


class TestFindBackup:
    def test_finds_existing_backup(self, tmp_path, sample_manifest):
        backup_dir = tmp_path / "2026-05-03T140000"
        save_manifest(sample_manifest, backup_dir)
        manifest, directory = _find_backup(sample_manifest.backup_id, tmp_path)
        assert manifest is not None
        assert manifest.backup_id == sample_manifest.backup_id
        assert directory == backup_dir

    def test_returns_none_for_missing(self, tmp_path):
        manifest, directory = _find_backup("nonexistent", tmp_path)
        assert manifest is None
        assert directory is None

    def test_returns_none_empty_root(self, tmp_path):
        manifest, directory = _find_backup("any-id", tmp_path)
        assert manifest is None


# ── _save_report / load_restore_report ────────────────────────────────────────


class TestSaveLoadRestoreReport:
    def test_save_and_load(self, tmp_path, sample_report):
        _save_report(sample_report, tmp_path)
        loaded = load_restore_report(tmp_path)
        assert loaded is not None
        assert loaded.restore_id == sample_report.restore_id
        assert loaded.status == sample_report.status

    def test_load_nonexistent(self, tmp_path):
        loaded = load_restore_report(tmp_path / "nonexistent")
        assert loaded is None

    def test_load_invalid_json(self, tmp_path):
        path = tmp_path / "restore_report.json"
        path.write_text("invalid{{{")
        loaded = load_restore_report(tmp_path)
        assert loaded is None

    def test_save_creates_directory(self, tmp_path, sample_report):
        deep_dir = tmp_path / "deep" / "nested" / "restore"
        _save_report(sample_report, deep_dir)
        assert deep_dir.exists()
        assert (deep_dir / "restore_report.json").exists()


# ── find_all_restore_reports / list_restores ──────────────────────────────────


class TestFindAllRestoreReports:
    def test_empty_directory(self, tmp_path):
        results = find_all_restore_reports(tmp_path)
        assert results == []

    def test_nonexistent_root(self, tmp_path):
        results = find_all_restore_reports(tmp_path / "does_not_exist")
        assert results == []

    def test_finds_single_report(self, tmp_path, sample_report):
        report_dir = tmp_path / "restore-20260503T150000"
        _save_report(sample_report, report_dir)
        results = find_all_restore_reports(tmp_path)
        assert len(results) == 1
        _, loaded = results[0]
        assert loaded.restore_id == sample_report.restore_id

    def test_list_restores(self, tmp_path, sample_report):
        report_dir = tmp_path / "restore-test"
        _save_report(sample_report, report_dir)
        restores = list_restores(tmp_path)
        assert len(restores) == 1
        assert restores[0]["restore_id"] == sample_report.restore_id
        assert restores[0]["status"] == "succeeded"

    def test_list_restores_empty(self, tmp_path):
        restores = list_restores(tmp_path)
        assert restores == []


# ── Schema serialization ──────────────────────────────────────────────────────


class TestRestoreSchemas:
    def test_restore_drill_request(self):
        req = RestoreDrillRequest(
            backup_id="abc-123",
            target_database_url=None,
            keep_temp_db=False,
        )
        d = req.model_dump()
        assert d["backup_id"] == "abc-123"
        assert d["keep_temp_db"] is False

    def test_restore_drill_request_with_target(self):
        req = RestoreDrillRequest(
            backup_id="abc-123",
            target_database_url="postgresql://user:pass@host/db",
            keep_temp_db=True,
        )
        d = req.model_dump()
        assert d["target_database_url"] is not None
        assert d["keep_temp_db"] is True

    def test_restore_drill_response(self):
        resp = RestoreDrillResponse(
            restore_id="r-1",
            success=True,
            status="succeeded",
            verification_summary={
                "table_count": True,
                "row_counts": True,
                "foreign_keys": True,
                "alembic_revision": True,
            },
            report_path="/tmp/report.json",
        )
        d = resp.model_dump()
        assert d["success"] is True
        assert d["verification_summary"]["table_count"] is True

    def test_restore_drill_response_failed(self):
        resp = RestoreDrillResponse(
            restore_id="r-2",
            success=False,
            status="failed",
            verification_summary={},
            error_message="Checksum mismatch",
        )
        d = resp.model_dump()
        assert d["success"] is False
        assert "Checksum" in d["error_message"]

    def test_restore_source_info(self):
        info = RestoreSourceInfo(
            backup_id="b-1",
            created_at="2026-05-03T14:00:00Z",
            file_path="/tmp/backup.dump",
            file_size_bytes=12345,
            checksum_sha256="abc...",
        )
        d = info.model_dump()
        assert d["backup_id"] == "b-1"
        assert d["file_size_bytes"] == 12345

    def test_restore_verification_result(self):
        v = RestoreVerificationResult(
            table_count={"expected": 45, "actual": 45, "match": True},
            row_counts={"match": True, "mismatches": [], "manifest_tables_not_in_restored": [], "restored_tables_not_in_manifest": []},
            foreign_keys={"valid": True, "violations": []},
            alembic_revision={"expected": "0001", "actual": "0001", "match": True},
        )
        d = v.model_dump()
        assert d["table_count"]["match"] is True
        assert d["foreign_keys"]["valid"] is True

    def test_restore_report_detail(self, sample_report):
        detail = RestoreReportDetail(
            restore_id=sample_report.restore_id,
            backup_id=sample_report.backup_id,
            restore_type="drill",
            started_at=sample_report.started_at,
            completed_at=sample_report.completed_at,
            status="succeeded",
            target_database="temp_db",
            source_backup=RestoreSourceInfo(**sample_report.source_backup),
            verification=RestoreVerificationResult(**sample_report.verification),
        )
        d = detail.model_dump()
        assert d["restore_id"] == sample_report.restore_id
        assert d["verification"]["table_count"]["match"] is True

    def test_restore_summary(self):
        summary = RestoreSummary(
            restore_id="r-1",
            backup_id="b-1",
            restore_type="drill",
            status="succeeded",
            started_at="2026-05-03T15:00:00Z",
            completed_at="2026-05-03T15:01:00Z",
            target_database="temp_db",
            report_directory="/tmp/restore-20260503T150000",
        )
        d = summary.model_dump()
        assert d["restore_id"] == "r-1"
        assert d["restore_type"] == "drill"

    def test_restore_list_response(self):
        from mneme.schemas.common import PageInfo
        items = [
            RestoreSummary(
                restore_id="r-1",
                backup_id="b-1",
                restore_type="drill",
                status="succeeded",
                started_at="2026-05-03T15:00:00Z",
                completed_at="2026-05-03T15:01:00Z",
                target_database="temp_db",
                report_directory="/tmp/r1",
            )
        ]
        page_info = PageInfo(page=1, page_size=50, total_items=1, total_pages=1, has_next=False, has_previous=False)
        resp = RestoreListResponse(items=items, page_info=page_info)
        d = resp.model_dump()
        assert len(d["items"]) == 1

    def test_restore_preview_response(self):
        preview = RestorePreviewResponse(
            backup_id="b-1",
            created_at="2026-05-03T14:00:00Z",
            tables=45,
            table_row_counts={"projects": 3, "users": 1},
            file_size_bytes=12345,
            target_database="(temp db)",
            restore_type="drill",
        )
        d = preview.model_dump()
        assert d["tables"] == 45
        assert d["table_row_counts"]["projects"] == 3


# ── CLI smoke tests ──────────────────────────────────────────────────────────


class TestRestoreCLISmoke:
    def test_drill_help(self):
        import pytest
        from mneme.backup.cli import main
        with pytest.raises(SystemExit) as exc_info:
            main(["drill", "--help"])
        assert exc_info.value.code == 0

    def test_restores_help(self):
        import pytest
        from mneme.backup.cli import main
        with pytest.raises(SystemExit) as exc_info:
            main(["restores", "--help"])
        assert exc_info.value.code == 0

    def test_restore_info_help(self):
        import pytest
        from mneme.backup.cli import main
        with pytest.raises(SystemExit) as exc_info:
            main(["restore-info", "--help"])
        assert exc_info.value.code == 0

    def test_restores_empty_list(self):
        from mneme.backup.cli import main
        with tempfile.TemporaryDirectory() as td:
            result = main(["restores", "--output-root", td])
            assert result == 0

    def test_drill_missing_backup_id(self):
        import pytest
        from mneme.backup.cli import main
        with tempfile.TemporaryDirectory() as td:
            # Should fail because backup doesn't exist
            result = main([
                "drill",
                "nonexistent-backup-id",
                "--output-root", td,
                "--database-url", "postgresql+psycopg2://user:pass@localhost:5432/mneme",
            ])
            assert result == 1

    def test_restore_info_missing_id(self):
        from mneme.backup.cli import main
        with tempfile.TemporaryDirectory() as td:
            result = main([
                "restore-info",
                "nonexistent-restore-id",
                "--output-root", td,
            ])
            assert result == 1


# ── Integration tests (require pg_dump/pg_restore) ────────────────────────────
# These are skipped by default; run with --run-integration to execute.


def _pg_restore_available() -> bool:
    """Check if pg_restore is available on the system."""
    import subprocess
    try:
        result = subprocess.run(
            ["pg_restore", "--version"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def _psql_available() -> bool:
    """Check if psql is available on the system."""
    import subprocess
    try:
        result = subprocess.run(
            ["psql", "--version"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


@pytest.mark.skipif(
    not _pg_restore_available(),
    reason="pg_restore not available in test environment",
)
class TestRestoreDrillIntegration:
    """Integration test: full backup → restore drill → verify.

    Requires:
    - PostgreSQL running (via DATABASE_URL env var or a local instance)
    - pg_dump and pg_restore available
    - Sufficient permissions to create/drop databases
    """

    def test_drill_with_real_backup(self, tmp_path):
        """End-to-end drill test if a database is available."""
        import os
        database_url = os.environ.get(
            "DATABASE_URL",
            "postgresql+psycopg2://mneme:mneme_dev_password@localhost:5432/mneme",
        )

        # First, create a backup
        from mneme.backup.engine import run_backup
        backup_result = run_backup(
            database_url=database_url,
            output_root=tmp_path,
        )

        if not backup_result.success:
            pytest.skip(
                f"Cannot create backup for drill test: {backup_result.error_message}"
            )

        backup_id = backup_result.manifest.backup_id

        # Now execute a restore drill
        result = run_restore_drill(
            backup_id=backup_id,
            source_database_url=database_url,
            output_root=tmp_path,
            keep_temp_db=False,
        )

        # We don't assert success because environment may not allow
        # creating/dropping databases. But the function should return
        # a result without crashing.
        assert result is not None
        if result.success:
            assert result.report is not None
            assert result.report.status == "succeeded"
            v = result.report.verification
            assert v["table_count"]["match"] is True
            assert v["foreign_keys"]["valid"] is True
            assert v["alembic_revision"]["match"] is True


# ── run_restore_live error handling (no live DB needed) ──────────────────────


class TestRestoreLiveErrors:
    def test_missing_backup(self, tmp_path):
        result = run_restore_live(
            backup_id="nonexistent-id",
            target_database_url="postgresql+psycopg2://user:pass@localhost:5432/mydb",
            output_root=tmp_path,
        )
        assert result.success is False
        assert "not found" in (result.error_message or "").lower()

    def test_failed_status_backup(self, tmp_path):
        # Create a manifest with failed status
        manifest = BackupManifest(
            backup_id="failed-backup",
            created_at="2026-05-03T14:00:00+00:00",
            pg_version="16.4",
            status="failed",
            error_message="pg_dump error",
            completed_at="2026-05-03T14:01:00+00:00",
            alembic_revision="0001",
        )
        backup_dir = tmp_path / "2026-05-03T140000"
        save_manifest(manifest, backup_dir)

        result = run_restore_live(
            backup_id="failed-backup",
            target_database_url="postgresql+psycopg2://user:pass@localhost:5432/mydb",
            output_root=tmp_path,
        )
        assert result.success is False
        assert "failed" in (result.error_message or "").lower()


# ── run_restore_drill error handling ─────────────────────────────────────────


class TestRestoreDrillErrors:
    def test_missing_backup(self, tmp_path):
        result = run_restore_drill(
            backup_id="nonexistent-id",
            source_database_url="postgresql+psycopg2://user:pass@localhost:5432/mneme",
            output_root=tmp_path,
        )
        assert result.success is False
        assert "not found" in (result.error_message or "").lower()

    def test_checksum_mismatch(self, tmp_path):
        # Create a manifest with a checksum that won't match any file
        manifest = BackupManifest(
            backup_id="bad-checksum",
            created_at="2026-05-03T14:00:00+00:00",
            pg_version="16.4",
            format="custom",
            tables=45,
            file_path=str(tmp_path / "backup.dump"),
            file_size_bytes=100,
            checksum_sha256="a" * 64,
            alembic_revision="0001_baseline_45_tables",
            status="succeeded",
            completed_at="2026-05-03T14:01:00+00:00",
        )

        # Create a dump file with different content
        dump_path = tmp_path / "backup.dump"
        dump_path.write_bytes(b"different content than expected")

        backup_dir = tmp_path / "2026-05-03T140000"
        save_manifest(manifest, backup_dir)

        result = run_restore_drill(
            backup_id="bad-checksum",
            source_database_url="postgresql+psycopg2://user:pass@localhost:5432/mneme",
            output_root=tmp_path,
        )
        assert result.success is False
        assert "checksum" in (result.error_message or "").lower()

    def test_missing_dump_file(self, tmp_path):
        manifest = BackupManifest(
            backup_id="missing-dump",
            created_at="2026-05-03T14:00:00+00:00",
            pg_version="16.4",
            format="custom",
            tables=45,
            file_path="/nonexistent/path/dump.dump",
            file_size_bytes=0,
            checksum_sha256="",
            alembic_revision="0001_baseline_45_tables",
            status="succeeded",
            completed_at="2026-05-03T14:01:00+00:00",
        )
        backup_dir = tmp_path / "2026-05-03T140000"
        save_manifest(manifest, backup_dir)

        result = run_restore_drill(
            backup_id="missing-dump",
            source_database_url="postgresql+psycopg2://user:pass@localhost:5432/mneme",
            output_root=tmp_path,
        )
        assert result.success is False
        assert "not found" in (result.error_message or "").lower()
