"""Project backend configuration — per-project search backend toggles."""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session


BACKEND_TYPES = ["fulltext", "vector", "graph"]


def ensure_project_backend(
    db: Session, project_id: UUID, backend_type: str, enabled: bool = True
) -> UUID:
    """Create a project_backends row if it does not exist, return the id."""
    row = db.execute(
        text("""
            INSERT INTO project_backends (project_id, backend_type, enabled)
            VALUES (:pid, :btype, :enabled)
            ON CONFLICT (project_id, backend_type) DO UPDATE
                SET enabled = EXCLUDED.enabled
            RETURNING id
        """),
        {"pid": project_id, "btype": backend_type, "enabled": enabled},
    ).scalar()
    return row


def init_default_backends(db: Session, project_id: UUID) -> None:
    """Create the mandatory fulltext backend + user-configurable vector/graph."""
    ensure_project_backend(db, project_id, "fulltext", True)
    ensure_project_backend(db, project_id, "vector", False)
    ensure_project_backend(db, project_id, "graph", False)


def get_project_backends(db: Session, project_id: UUID) -> list[dict]:
    rows = db.execute(
        text("""
            SELECT id, backend_type, enabled, config_json, created_at
            FROM project_backends
            WHERE project_id = :pid
            ORDER BY backend_type
        """),
        {"pid": project_id},
    ).mappings().all()
    return [dict(r) for r in rows]


def set_backend_enabled(
    db: Session, project_id: UUID, backend_type: str, enabled: bool
) -> None:
    """Enable or disable a backend. When disabling, mark all document
    index_states for that backend as 'disabled'.  When enabling, mark
    them as 'stale' so they will be rebuilt."""
    db.execute(
        text("""
            UPDATE project_backends
            SET enabled = :enabled
            WHERE project_id = :pid AND backend_type = :btype
        """),
        {"pid": project_id, "btype": backend_type, "enabled": enabled},
    )
    new_state = "stale" if enabled else "disabled"
    db.execute(
        text("""
            UPDATE document_index_states
            SET state = :state, updated_at = now()
            WHERE document_id IN (
                SELECT document_id FROM knowledge_documents
                WHERE project_id = :pid
            )
            AND backend_type = :btype
        """),
        {"pid": project_id, "btype": backend_type, "state": new_state},
    )
