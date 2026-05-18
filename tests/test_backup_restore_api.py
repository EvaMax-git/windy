"""P2-16 Backup/Restore Management API tests.

Tests cover:
1. Schema serialization for P2-16 request/response models
2. Backup trigger request/response validation
3. Restore submit request/response validation
4. Restore preview response validation
5. Job status response validation
6. API route registration (smoke)
7. Restore preview logic (unit)
8. Jobs data-access layer (unit, requires DB)
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from mneme.schemas.backup import (
    BackupTriggerRequest,
    BackupTriggerResponse,
    RestoreSubmitRequest,
    RestoreSubmitResponse,
    RestoreDetailedPreview,
    TableComparisonItem,
    JobLogEntry,
    JobStatusResponse,
)
from mneme.restore.preview import (
    RestorePreview,
    TableComparison,
    preview_restore,
)
from mneme.db.jobs import (
    create_job,
    get_job_by_id,
    get_job_logs,
    add_job_log,
    update_job_completed,
    update_job_running,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Schema serialization tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestBackupTriggerSchemas:
    """P2-16 BackupTrigger request/response schema tests."""

    def test_trigger_request_defaults(self) -> None:
        req = BackupTriggerRequest()
        assert req.database_url is None
        assert req.backup_id is None

    def test_trigger_request_with_fields(self) -> None:
        req = BackupTriggerRequest(
            database_url="postgresql://localhost:5432/test",
            backup_id="abc-123-def",
        )
        assert req.database_url == "postgresql://localhost:5432/test"
        assert req.backup_id == "abc-123-def"

    def test_trigger_response(self) -> None:
        job_id = uuid4()
        resp = BackupTriggerResponse(
            backup_id="backup-001",
            job_id=job_id,
            status="pending",
            message="Backup job created",
        )
        data = resp.model_dump(mode="json")
        assert data["backup_id"] == "backup-001"
        assert data["job_id"] == str(job_id)
        assert data["status"] == "pending"

    def test_trigger_response_json_serializable(self) -> None:
        import json
        resp = BackupTriggerResponse(
            backup_id="b-1",
            job_id=uuid4(),
            status="pending",
        )
        json.dumps(resp.model_dump(mode="json"))  # Should not raise


class TestRestoreSubmitSchemas:
    """P2-16 RestoreSubmit request/response schema tests."""

    def test_submit_request_minimal(self) -> None:
        req = RestoreSubmitRequest(backup_id="backup-001")
        assert req.backup_id == "backup-001"
        assert req.target_database_url is None
        assert req.clean is True

    def test_submit_request_full(self) -> None:
        req = RestoreSubmitRequest(
            backup_id="backup-002",
            target_database_url="postgresql://localhost:5432/target",
            clean=False,
            reason="Emergency restore needed",
        )
        assert req.backup_id == "backup-002"
        assert req.target_database_url == "postgresql://localhost:5432/target"
        assert req.clean is False
        assert req.reason == "Emergency restore needed"

    def test_submit_response(self) -> None:
        review_id = uuid4()
        resp = RestoreSubmitResponse(
            backup_id="backup-003",
            review_item_id=review_id,
            status="pending",
        )
        data = resp.model_dump(mode="json")
        assert data["backup_id"] == "backup-003"
        assert data["review_item_id"] == str(review_id)
        assert data["status"] == "pending"


class TestRestorePreviewSchemas:
    """P2-16 RestoreDetailedPreview schema tests."""

    def test_table_comparison_item(self) -> None:
        item = TableComparisonItem(
            table_name="projects",
            backup_rows=10,
            live_rows=8,
            difference=2,
            exists_in_live=True,
            will_be="overwritten",
        )
        data = item.model_dump(mode="json")
        assert data["table_name"] == "projects"
        assert data["backup_rows"] == 10
        assert data["live_rows"] == 8
        assert data["difference"] == 2
        assert data["will_be"] == "overwritten"

    def test_restore_detailed_preview(self) -> None:
        comparisons = [
            TableComparisonItem(
                table_name="projects",
                backup_rows=5,
                live_rows=5,
                difference=0,
                will_be="unchanged",
            ),
            TableComparisonItem(
                table_name="users",
                backup_rows=1,
                live_rows=0,
                difference=1,
                exists_in_live=False,
                will_be="created",
            ),
        ]
        preview = RestoreDetailedPreview(
            backup_id="b-001",
            backup_created_at="2026-05-03T12:00:00Z",
            backup_tables=45,
            live_tables=44,
            table_comparisons=comparisons,
            total_rows_backup=6,
            total_rows_live=5,
            will_overwrite_tables=0,
            will_create_tables=1,
            will_drop_tables=0,
            warnings=["1 table(s) will be created"],
        )
        data = preview.model_dump(mode="json")
        assert data["backup_id"] == "b-001"
        assert data["backup_tables"] == 45
        assert data["live_tables"] == 44
        assert len(data["table_comparisons"]) == 2
        assert data["will_create_tables"] == 1
        assert "1 table(s) will be created" in data["warnings"]


class TestJobSchemas:
    """P2-16 JobStatus / JobLogEntry schema tests."""

    def test_job_log_entry(self) -> None:
        log = JobLogEntry(
            job_log_id=uuid4(),
            job_id=uuid4(),
            step="backup.starting",
            level="info",
            message="Starting pg_dump",
            attempt_no=0,
        )
        data = log.model_dump(mode="json")
        assert data["step"] == "backup.starting"
        assert data["level"] == "info"

    def test_job_status_response(self) -> None:
        logs = [
            JobLogEntry(
                job_log_id=uuid4(),
                job_id=uuid4(),
                step="backup.completed",
                level="info",
                message="Done",
                attempt_no=0,
            ),
        ]
        resp = JobStatusResponse(
            job_id=uuid4(),
            job_type="backup",
            job_key="backup.abc",
            status="succeeded",
            logs=logs,
        )
        data = resp.model_dump(mode="json")
        assert data["job_type"] == "backup"
        assert data["status"] == "succeeded"
        assert len(data["logs"]) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# Restore preview logic tests (unit)
# ═══════════════════════════════════════════════════════════════════════════════


class TestRestorePreviewLogic:
    """P2-16 RestorePreview dataclass and unit logic tests."""

    def test_table_comparison_created(self) -> None:
        tc = TableComparison(
            table_name="new_table",
            backup_rows=10,
            live_rows=0,
            difference=10,
            exists_in_live=False,
            will_be="created",
        )
        assert tc.will_be == "created"
        assert tc.exists_in_live is False
        assert tc.difference == 10

    def test_table_comparison_overwritten(self) -> None:
        tc = TableComparison(
            table_name="projects",
            backup_rows=5,
            live_rows=3,
            difference=2,
            exists_in_live=True,
            will_be="overwritten",
        )
        assert tc.will_be == "overwritten"
        assert tc.difference == 2

    def test_table_comparison_unchanged(self) -> None:
        tc = TableComparison(
            table_name="users",
            backup_rows=1,
            live_rows=1,
            difference=0,
            exists_in_live=True,
            will_be="unchanged",
        )
        assert tc.will_be == "unchanged"
        assert tc.difference == 0

    def test_restore_preview_to_dict(self) -> None:
        tc = TableComparison(
            table_name="projects",
            backup_rows=5,
            live_rows=5,
            difference=0,
            will_be="unchanged",
        )
        preview = RestorePreview(
            backup_id="b-001",
            backup_created_at="2026-01-01T00:00:00Z",
            backup_tables=45,
            live_tables=45,
            table_comparisons=[tc],
            total_rows_backup=5,
            total_rows_live=5,
        )
        d = preview.to_dict()
        assert d["backup_id"] == "b-001"
        assert d["backup_tables"] == 45
        assert len(d["table_comparisons"]) == 1

    def test_preview_not_found(self) -> None:
        preview = preview_restore("nonexistent-backup-id-12345")
        assert preview.error is not None
        assert "not found" in preview.error.lower()

    def test_preview_empty(self) -> None:
        """Ensure preview doesn't crash with empty data."""
        preview = RestorePreview(backup_id="empty")
        d = preview.to_dict()
        assert d["backup_id"] == "empty"
        assert d["table_comparisons"] == []


# ═══════════════════════════════════════════════════════════════════════════════
# Jobs data-access tests (requires DB)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
class TestJobsDB:
    """P2-16 Jobs data-access layer integration tests."""

    def test_create_job(self) -> None:
        """Create a backup-type job and verify it exists."""
        try:
            job = create_job(
                job_type="backup",
                job_key=f"test.backup.{uuid4()}",
                input_payload={"backup_id": "test-backup-001"},
            )
        except Exception:
            pytest.skip("Database not available")

        assert job is not None
        assert job["job_type"] == "backup"
        assert job["status"] == "pending"
        assert "job_id" in job

        # Verify retrieval
        job_id = UUID(job["job_id"])
        retrieved = get_job_by_id(job_id)
        assert retrieved is not None
        assert retrieved["job_type"] == "backup"

    def test_job_lifecycle(self) -> None:
        """Test full job lifecycle: create → running → completed."""
        try:
            job = create_job(
                job_type="restore",
                job_key=f"test.restore.{uuid4()}",
                input_payload={"backup_id": "test-002"},
            )
        except Exception:
            pytest.skip("Database not available")

        job_id = UUID(job["job_id"])
        assert job["status"] == "pending"

        # Move to running
        assert update_job_running(job_id) is True
        running = get_job_by_id(job_id)
        assert running is not None
        assert running["status"] == "running"
        assert running["started_at"] is not None

        # Add logs
        log_id = add_job_log(job_id, step="restore.starting", message="Starting restore")
        assert log_id is not None

        # Complete successfully
        assert update_job_completed(
            job_id,
            success=True,
            output={"tables_restored": 45},
        ) is True
        completed = get_job_by_id(job_id)
        assert completed is not None
        assert completed["status"] == "succeeded"
        assert completed["finished_at"] is not None

        # Check logs
        logs = get_job_logs(job_id)
        assert len(logs) >= 1
        assert logs[0]["step"] == "restore.starting"

    def test_job_failure(self) -> None:
        """Test job failure recording."""
        try:
            job = create_job(
                job_type="backup",
                job_key=f"test.backup.fail.{uuid4()}",
                input_payload={"backup_id": "test-fail-001"},
            )
        except Exception:
            pytest.skip("Database not available")

        job_id = UUID(job["job_id"])
        update_job_running(job_id)
        assert update_job_completed(
            job_id,
            success=False,
            error_message="pg_dump: connection refused",
        ) is True

        failed = get_job_by_id(job_id)
        assert failed is not None
        assert failed["status"] == "failed"
        assert failed["last_error"] is not None
        assert "connection refused" in str(failed["last_error"])


# ═══════════════════════════════════════════════════════════════════════════════
# API route smoke tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestBackupRoutesSmoke:
    """Verify the API router is properly configured (no FastAPI app required)."""

    def test_router_exists(self) -> None:
        from mneme.api.routes.system.backup import router
        assert router is not None
        assert router.prefix == "/admin"

    def test_routes_registered(self) -> None:
        from mneme.api.routes.system.backup import router
        route_paths = [r.path for r in router.routes]
        # P2-14 endpoints
        assert "/admin/backups" in route_paths or "/backups" in route_paths
        # P2-16 endpoints
        for expected in ["/backup", "/restore", "/jobs/{job_id}"]:
            found = any(expected in p for p in route_paths)
            assert found, f"Route '{expected}' not found in {route_paths}"
