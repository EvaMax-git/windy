"""P2-14 Backup tests — manifest, engine, CLI, integrity verification.

Tests cover:
1. Manifest creation and JSON roundtrip
2. Manifest validation
3. Manifest file I/O (save/load)
4. Checksum computation and verification
5. find_all_manifests scanning
6. Backup engine (requires pg_dump, skipped if unavailable)
7. BackupResult dataclass
8. Table name list integrity
9. API schema serialization
10. CLI argument parsing
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest

from mneme.backup.manifest import (
    MNAME_TABLES,
    BackupManifest,
    compute_sha256,
    find_all_manifests,
    load_manifest,
    save_manifest,
    validate_manifest,
    verify_checksum,
)
from mneme.backup.engine import (
    BackupResult,
    _default_backup_root,
    _summarize_row_counts,
    verify_backup,
)
from mneme.schemas.backup import (
    BackupDetail,
    BackupListResponse,
    BackupSummary,
    BackupVerifyResponse,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def sample_manifest_dict() -> dict:
    return {
        "backup_id": str(uuid4()),
        "created_at": "2026-05-03T12:00:00+00:00",
        "pg_version": "16.4",
        "format": "custom",
        "tables": 45,
        "table_row_counts": {
            "projects": 3,
            "users": 1,
            "user_sessions": 2,
            "agents": 5,
            "agent_tokens": 2,
            "audit_events": 150,
            "events": 80,
            "event_deliveries": 80,
            "dead_letters": 2,
            "review_items": 5,
        },
        "file_path": "/tmp/backup.dump",
        "file_size_bytes": 123456,
        "checksum_sha256": "a" * 64,
        "alembic_revision": "0001_baseline_45_tables",
        "status": "succeeded",
        "error_message": None,
        "completed_at": "2026-05-03T12:01:00+00:00",
        "dump_command": "pg_dump -Fc ...",
        "env_info": {"python_version": "3.12.0"},
    }


@pytest.fixture
def sample_manifest(sample_manifest_dict) -> BackupManifest:
    return BackupManifest.from_dict(sample_manifest_dict)


# ── Manifest creation and serialization ───────────────────────────────────────


class TestManifestCreation:
    def test_create_from_dict(self, sample_manifest_dict):
        m = BackupManifest.from_dict(sample_manifest_dict)
        assert m.backup_id == sample_manifest_dict["backup_id"]
        assert m.pg_version == "16.4"
        assert m.tables == 45
        assert m.format == "custom"
        assert m.status == "succeeded"

    def test_default_values(self):
        m = BackupManifest(
            backup_id="test-1",
            created_at="2026-01-01T00:00:00Z",
            pg_version="16.0",
        )
        assert m.format == "custom"
        assert m.tables == 45
        assert m.file_path == "backup.dump"
        assert m.file_size_bytes == 0
        assert m.checksum_sha256 == ""
        assert m.alembic_revision == ""
        assert m.status == "succeeded"
        assert m.table_row_counts == {}

    def test_to_dict_roundtrip(self, sample_manifest):
        d = sample_manifest.to_dict()
        m2 = BackupManifest.from_dict(d)
        assert m2.backup_id == sample_manifest.backup_id
        assert m2.checksum_sha256 == sample_manifest.checksum_sha256

    def test_json_roundtrip(self, sample_manifest):
        json_str = sample_manifest.to_json()
        m2 = BackupManifest.from_json(json_str)
        assert m2.backup_id == sample_manifest.backup_id
        assert m2.pg_version == sample_manifest.pg_version
        assert m2.table_row_counts == sample_manifest.table_row_counts

    def test_json_is_valid(self, sample_manifest):
        json_str = sample_manifest.to_json()
        parsed = json.loads(json_str)
        assert parsed["backup_id"] == sample_manifest.backup_id


# ── Manifest validation ───────────────────────────────────────────────────────


class TestManifestValidation:
    def test_valid_manifest_passes(self, sample_manifest):
        issues = validate_manifest(sample_manifest)
        assert issues == []

    def test_empty_backup_id_fails(self, sample_manifest):
        sample_manifest.backup_id = ""
        issues = validate_manifest(sample_manifest)
        assert any("backup_id" in i for i in issues)

    def test_wrong_table_count_fails(self, sample_manifest):
        sample_manifest.tables = 10
        issues = validate_manifest(sample_manifest)
        assert any("tables" in i for i in issues)

    def test_missing_alembic_revision_fails(self, sample_manifest):
        sample_manifest.alembic_revision = ""
        issues = validate_manifest(sample_manifest)
        assert any("alembic_revision" in i for i in issues)

    def test_unknown_table_in_row_counts(self, sample_manifest):
        # Row count validation checks for unknown table names
        sample_manifest.table_row_counts = {"projects": 1, "nonexistent_table": 5}
        issues = validate_manifest(sample_manifest)
        assert any("unknown table names" in i for i in issues)

    def test_empty_row_counts_is_fine(self, sample_manifest):
        sample_manifest.table_row_counts = {}
        issues = validate_manifest(sample_manifest)
        # Empty dict means no validation of table names
        assert issues == []


# ── Manifest file I/O ─────────────────────────────────────────────────────────


class TestManifestFileIO:
    def test_save_and_load(self, sample_manifest, tmp_path):
        save_manifest(sample_manifest, tmp_path)
        loaded = load_manifest(tmp_path)
        assert loaded is not None
        assert loaded.backup_id == sample_manifest.backup_id
        assert loaded.checksum_sha256 == sample_manifest.checksum_sha256

    def test_load_nonexistent(self, tmp_path):
        loaded = load_manifest(tmp_path / "nonexistent")
        assert loaded is None

    def test_load_invalid_json(self, tmp_path):
        path = tmp_path / "manifest.json"
        path.write_text("not valid json{{{")
        loaded = load_manifest(tmp_path)
        assert loaded is None

    def test_save_creates_directory(self, tmp_path, sample_manifest):
        deep_dir = tmp_path / "deep" / "nested" / "path"
        save_manifest(sample_manifest, deep_dir)
        assert deep_dir.exists()
        assert (deep_dir / "manifest.json").exists()


# ── Checksum ──────────────────────────────────────────────────────────────────


class TestChecksum:
    def test_compute_sha256(self, tmp_path):
        file_path = tmp_path / "test.bin"
        file_path.write_bytes(b"hello world")
        digest = compute_sha256(file_path)
        assert len(digest) == 64
        # Known SHA-256 of "hello world"
        expected = "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
        assert digest == expected

    def test_verify_checksum_match(self, tmp_path):
        file_path = tmp_path / "test.bin"
        file_path.write_bytes(b"hello world")
        digest = compute_sha256(file_path)
        manifest = BackupManifest(
            backup_id="test",
            created_at="2026-01-01T00:00:00Z",
            pg_version="16.0",
            checksum_sha256=digest,
            file_path=str(file_path),
            file_size_bytes=11,
        )
        assert verify_checksum(manifest, file_path) is True

    def test_verify_checksum_mismatch(self, tmp_path):
        file_path = tmp_path / "test.bin"
        file_path.write_bytes(b"hello world")
        manifest = BackupManifest(
            backup_id="test",
            created_at="2026-01-01T00:00:00Z",
            pg_version="16.0",
            checksum_sha256="b" * 64,  # wrong checksum
            file_path=str(file_path),
        )
        assert verify_checksum(manifest, file_path) is False


# ── find_all_manifests ────────────────────────────────────────────────────────


class TestFindAllManifests:
    def test_empty_directory(self, tmp_path):
        results = find_all_manifests(tmp_path)
        assert results == []

    def test_nonexistent_root(self, tmp_path):
        results = find_all_manifests(tmp_path / "does_not_exist")
        assert results == []

    def test_finds_single_manifest(self, tmp_path, sample_manifest):
        backup_dir = tmp_path / "2026-05-03T120000"
        save_manifest(sample_manifest, backup_dir)
        results = find_all_manifests(tmp_path)
        assert len(results) == 1
        _, loaded = results[0]
        assert loaded.backup_id == sample_manifest.backup_id

    def test_finds_multiple_sorted_newest_first(self, tmp_path):
        m1 = BackupManifest(
            backup_id="older",
            created_at="2026-01-01T00:00:00+00:00",
            pg_version="16.0",
        )
        m2 = BackupManifest(
            backup_id="newer",
            created_at="2026-06-01T00:00:00+00:00",
            pg_version="16.4",
        )

        save_manifest(m1, tmp_path / "2026-01-01T000000")
        save_manifest(m2, tmp_path / "2026-06-01T000000")

        results = find_all_manifests(tmp_path)
        assert len(results) == 2
        # Newest first
        _, first = results[0]
        assert first.backup_id == "newer"


# ── Engine helpers ────────────────────────────────────────────────────────────


class TestEngineHelpers:
    def test_summarize_row_counts(self):
        counts = {"a": 0, "b": 5, "c": 10}
        summary = _summarize_row_counts(counts)
        assert summary["total_rows"] == 15
        assert summary["non_empty_tables"] == 2

    def test_summarize_empty(self):
        summary = _summarize_row_counts({})
        assert summary["total_rows"] == 0
        assert summary["non_empty_tables"] == 0

    def test_default_backup_root_is_path(self):
        root = _default_backup_root()
        assert isinstance(root, Path)
        assert root.name == "backups"


# ── BackupResult ──────────────────────────────────────────────────────────────


class TestBackupResult:
    def test_success_result(self, sample_manifest):
        result = BackupResult(
            success=True,
            manifest=sample_manifest,
            output_dir=Path("/tmp/backup"),
        )
        assert result.success is True
        assert result.manifest is sample_manifest
        assert result.error_message is None

    def test_failure_result(self):
        result = BackupResult(
            success=False,
            output_dir=Path("/tmp/backup"),
            error_message="pg_dump not found",
        )
        assert result.success is False
        assert result.manifest is None
        assert "pg_dump" in (result.error_message or "")


# ── verify_backup function ────────────────────────────────────────────────────


class TestVerifyBackup:
    def test_valid_backup_verifies(self, tmp_path, sample_manifest):
        # Create a real dump file
        dump_path = tmp_path / "backup.dump"
        dump_path.write_bytes(b"fake dump content")
        digest = compute_sha256(dump_path)

        sample_manifest.file_path = str(dump_path)
        sample_manifest.file_size_bytes = dump_path.stat().st_size
        sample_manifest.checksum_sha256 = digest

        result = verify_backup(sample_manifest)
        assert result["valid"] is True
        assert result["issues"] == []

    def test_missing_file(self, sample_manifest):
        sample_manifest.file_path = "/nonexistent/path/dump"
        result = verify_backup(sample_manifest)
        assert result["valid"] is False
        assert any("not found" in i for i in result["issues"])

    def test_size_mismatch(self, tmp_path, sample_manifest):
        dump_path = tmp_path / "backup.dump"
        dump_path.write_bytes(b"content")
        sample_manifest.file_path = str(dump_path)
        sample_manifest.file_size_bytes = 99999  # wrong size
        sample_manifest.checksum_sha256 = compute_sha256(dump_path)

        result = verify_backup(sample_manifest)
        assert any("size mismatch" in i.lower() for i in result["issues"])

    def test_checksum_mismatch(self, tmp_path, sample_manifest):
        dump_path = tmp_path / "backup.dump"
        dump_path.write_bytes(b"content")
        sample_manifest.file_path = str(dump_path)
        sample_manifest.file_size_bytes = dump_path.stat().st_size
        sample_manifest.checksum_sha256 = "c" * 64  # wrong

        result = verify_backup(sample_manifest)
        assert any("checksum mismatch" in i.lower() for i in result["issues"])


# ── Table list integrity ──────────────────────────────────────────────────────


class TestTableList:
    def test_exactly_45_tables(self):
        assert len(MNAME_TABLES) == 45

    def test_all_unique(self):
        assert len(set(MNAME_TABLES)) == 45

    def test_key_tables_present(self):
        required = [
            "projects", "users", "audit_events", "events",
            "event_deliveries", "dead_letters", "review_items",
            "credential_vault", "vault_access_logs", "api_call_logs",
            "providers", "provider_models", "capabilities",
            "capability_bindings", "usage_limits", "budget_tracking",
            "jobs", "job_logs",
        ]
        for t in required:
            assert t in MNAME_TABLES, f"Missing required table: {t}"


# ── API Schemas ───────────────────────────────────────────────────────────────


class TestBackupSchemas:
    def test_backup_summary_from_dict(self):
        summary = BackupSummary(
            backup_id="abc-123",
            created_at="2026-05-03T12:00:00Z",
            pg_version="16.4",
            status="succeeded",
            file_size_bytes=12345,
            alembic_revision="0001",
            tables=45,
            table_count_summary={"total_rows": 100, "non_empty_tables": 10},
            checksum_sha256="abcd1234...",
            backup_directory="/tmp/backups/2026-05-03T120000",
        )
        d = summary.model_dump()
        assert d["backup_id"] == "abc-123"
        assert d["status"] == "succeeded"

    def test_backup_detail_full(self):
        detail = BackupDetail(
            backup_id="abc-123",
            created_at="2026-05-03T12:00:00Z",
            pg_version="16.4",
            format="custom",
            tables=45,
            table_row_counts={"projects": 3},
            file_path="/tmp/backup.dump",
            file_size_bytes=12345,
            checksum_sha256="a" * 64,
            alembic_revision="0001_baseline_45_tables",
            status="succeeded",
            completed_at="2026-05-03T12:01:00Z",
        )
        d = detail.model_dump()
        assert d["tables"] == 45
        assert d["format"] == "custom"

    def test_verify_response(self):
        resp = BackupVerifyResponse(
            backup_id="abc-123",
            valid=True,
            issues=[],
            file_size_bytes=12345,
            checksum_match=True,
        )
        d = resp.model_dump()
        assert d["valid"] is True
        assert d["checksum_match"] is True

    def test_backup_list_response(self):
        from mneme.schemas.common import PageInfo
        items = [
            BackupSummary(
                backup_id="b1",
                created_at="2026-05-03T12:00:00Z",
                pg_version="16.4",
                status="succeeded",
                file_size_bytes=1000,
                alembic_revision="0001",
                tables=45,
                backup_directory="/tmp/b1",
            )
        ]
        page_info = PageInfo(page=1, page_size=50, total_items=1, total_pages=1, has_next=False, has_previous=False)
        resp = BackupListResponse(items=items, page_info=page_info)
        d = resp.model_dump()
        assert len(d["items"]) == 1


# ── CLI (basic smoke) ─────────────────────────────────────────────────────────


class TestCLISmoke:
    def test_main_help(self):
        import pytest
        from mneme.backup.cli import main
        with pytest.raises(SystemExit) as exc_info:
            main(["--help"])
        assert exc_info.value.code == 0

    def test_main_list_no_backups(self):
        from mneme.backup.cli import main
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            result = main(["list", "--output-root", td])
            assert result == 0

    def test_main_invalid_command(self):
        import pytest
        from mneme.backup.cli import main
        with pytest.raises(SystemExit) as exc_info:
            main(["invalid-cmd"])
        # argparse exits with code 2 for invalid choice
        assert exc_info.value.code == 2
