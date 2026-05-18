"""P2-14 Backup manifest — JSON schema, serialization, validation, and file I/O.

A manifest captures the metadata for a single backup run:
backup_id, timestamps, pg_version, table row counts, file path, checksum,
and the Alembic revision at the time of backup.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID


# ── Manifest data schema ──────────────────────────────────────────────────────

# The 45 table names in the Mneme v4.1.1 data model, in order.
MNAME_TABLES: list[str] = [
    "projects",
    "users",
    "user_sessions",
    "agents",
    "agent_tokens",
    "audit_events",
    "events",
    "event_deliveries",
    "dead_letters",
    "review_items",
    "conversations",
    "event_source",
    "messages",
    "raw_events",
    "memory_candidates",
    "memories",
    "memory_versions",
    "memory_sources",
    "memory_index_entries",
    "memory_relations",
    "knowledge_documents",
    "knowledge_blocks",
    "knowledge_chunks",
    "index_states",
    "source_maps",
    "providers",
    "provider_models",
    "capabilities",
    "capability_bindings",
    "credential_vault",
    "vault_access_logs",
    "api_call_logs",
    "usage_limits",
    "budget_tracking",
    "jobs",
    "job_logs",
    "pipeline_defs",
    "pipeline_runs",
    "inbox_items",
    "assets",
    "asset_metadata",
    "context_packs",
    "context_pack_items",
    "object_registry",
    "object_versions",
]

assert len(MNAME_TABLES) == 45, f"Expected 45 tables, got {len(MNAME_TABLES)}"


class BackupManifest:
    """In-memory representation of a backup manifest."""

    __slots__ = (
        "backup_id",
        "created_at",
        "pg_version",
        "format",
        "tables",
        "table_row_counts",
        "file_path",
        "file_size_bytes",
        "checksum_sha256",
        "alembic_revision",
        "status",
        "error_message",
        "completed_at",
        "dump_command",
        "env_info",
    )

    def __init__(
        self,
        *,
        backup_id: str,
        created_at: str,
        pg_version: str,
        format: str = "custom",
        tables: int = 45,
        table_row_counts: dict[str, int] | None = None,
        file_path: str = "backup.dump",
        file_size_bytes: int = 0,
        checksum_sha256: str = "",
        alembic_revision: str = "",
        status: str = "succeeded",
        error_message: str | None = None,
        completed_at: str | None = None,
        dump_command: str | None = None,
        env_info: dict[str, str] | None = None,
    ) -> None:
        self.backup_id = backup_id
        self.created_at = created_at
        self.pg_version = pg_version
        self.format = format
        self.tables = tables
        self.table_row_counts = table_row_counts or {}
        self.file_path = file_path
        self.file_size_bytes = file_size_bytes
        self.checksum_sha256 = checksum_sha256
        self.alembic_revision = alembic_revision
        self.status = status
        self.error_message = error_message
        self.completed_at = completed_at
        self.dump_command = dump_command
        self.env_info = env_info or {}

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-serializable dictionary."""
        return {
            "backup_id": self.backup_id,
            "created_at": self.created_at,
            "pg_version": self.pg_version,
            "format": self.format,
            "tables": self.tables,
            "table_row_counts": self.table_row_counts,
            "file_path": self.file_path,
            "file_size_bytes": self.file_size_bytes,
            "checksum_sha256": self.checksum_sha256,
            "alembic_revision": self.alembic_revision,
            "status": self.status,
            "error_message": self.error_message,
            "completed_at": self.completed_at,
            "dump_command": self.dump_command,
            "env_info": self.env_info,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BackupManifest:
        """Deserialize from a dictionary (e.g. parsed JSON)."""
        return cls(
            backup_id=data.get("backup_id", ""),
            created_at=data.get("created_at", ""),
            pg_version=data.get("pg_version", ""),
            format=data.get("format", "custom"),
            tables=data.get("tables", 45),
            table_row_counts=data.get("table_row_counts", {}),
            file_path=data.get("file_path", "backup.dump"),
            file_size_bytes=data.get("file_size_bytes", 0),
            checksum_sha256=data.get("checksum_sha256", ""),
            alembic_revision=data.get("alembic_revision", ""),
            status=data.get("status", "succeeded"),
            error_message=data.get("error_message"),
            completed_at=data.get("completed_at"),
            dump_command=data.get("dump_command"),
            env_info=data.get("env_info", {}),
        )

    def to_json(self) -> str:
        """Serialize to a pretty-printed JSON string."""
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)

    @classmethod
    def from_json(cls, json_str: str) -> BackupManifest:
        """Deserialize from a JSON string."""
        return cls.from_dict(json.loads(json_str))


# ── Manifest file I/O ─────────────────────────────────────────────────────────


def save_manifest(manifest: BackupManifest, directory: Path) -> Path:
    """Write *manifest* to ``manifest.json`` inside *directory*.

    Returns the path to the written file.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "manifest.json"
    path.write_text(manifest.to_json(), encoding="utf-8")
    return path


def load_manifest(directory: Path) -> BackupManifest | None:
    """Load ``manifest.json`` from *directory*.

    Returns ``None`` if the file does not exist or cannot be parsed.
    """
    path = directory / "manifest.json"
    if not path.exists():
        return None
    try:
        return BackupManifest.from_json(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def find_all_manifests(backups_root: Path) -> list[tuple[Path, BackupManifest]]:
    """Scan *backups_root* recursively for ``manifest.json`` files.

    Returns a list of ``(directory, manifest)`` tuples, sorted by
    ``created_at`` descending (newest first).
    """
    results: list[tuple[Path, BackupManifest]] = []
    if not backups_root.exists():
        return results

    for entry in sorted(backups_root.iterdir(), reverse=True):
        if entry.is_dir():
            manifest = load_manifest(entry)
            if manifest is not None:
                results.append((entry, manifest))
    return results


# ── Checksum helpers ──────────────────────────────────────────────────────────


def compute_sha256(file_path: Path) -> str:
    """Compute SHA-256 hex digest of *file_path*."""
    sha = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha.update(chunk)
    return sha.hexdigest()


def verify_checksum(manifest: BackupManifest, dump_path: Path) -> bool:
    """Verify that the dump file's SHA-256 matches the manifest."""
    actual = compute_sha256(dump_path)
    return actual == manifest.checksum_sha256


# ── Manifest validation ───────────────────────────────────────────────────────


def validate_manifest(manifest: BackupManifest) -> list[str]:
    """Validate *manifest* fields and return a list of issues (empty = valid)."""
    issues: list[str] = []

    if not manifest.backup_id:
        issues.append("backup_id is empty")
    if not manifest.created_at:
        issues.append("created_at is empty")
    if not manifest.pg_version:
        issues.append("pg_version is empty")
    if manifest.tables != 45:
        issues.append(f"tables count is {manifest.tables}, expected 45")
    if not manifest.file_path:
        issues.append("file_path is empty")
    if not manifest.alembic_revision:
        issues.append("alembic_revision is empty")

    # Validate any table names present in row counts are valid table names
    if manifest.table_row_counts:
        unknown = set(manifest.table_row_counts.keys()) - set(MNAME_TABLES)
        if unknown:
            issues.append(f"unknown table names in row counts: {sorted(unknown)}")

    return issues
