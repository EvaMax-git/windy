"""Per-document × per-backend index state tracking."""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session


def init_index_states(db: Session, document_id: UUID | str, project_id: UUID | str) -> None:
    """Create a row per enabled backend for a newly created document.

    Accepts both UUID objects and string representations.
    """
    db.execute(
        text("""
            INSERT INTO document_index_states
                (document_id, backend_type, state, target_version)
            SELECT CAST(:did AS uuid), pb.backend_type, 'pending', 0
            FROM project_backends pb
            WHERE pb.project_id = CAST(:pid AS uuid) AND pb.enabled = true
            ON CONFLICT (document_id, backend_type) DO NOTHING
        """),
        {"did": str(document_id), "pid": str(project_id)},
    )


def mark_stale(db: Session, document_id: UUID | str, target_version: int) -> None:
    """Mark all backend index states for this document as stale."""
    db.execute(
        text("""
            UPDATE document_index_states
            SET state = 'stale',
                target_version = :ver,
                updated_at = now()
            WHERE document_id = CAST(:did AS uuid)
              AND state IN ('ready', 'pending')
        """),
        {"did": str(document_id), "ver": target_version},
    )


def mark_stale_for_blocks(
    db: Session, document_id: UUID, block_ids: list[UUID] | None, target_version: int
) -> None:
    """Mark stale only for backends affected by the changed blocks.
    If block_ids is None or empty, mark all backends stale (full rebuild)."""
    if not block_ids:
        mark_stale(db, document_id, target_version)
        return
    # FTS and Vector: stale if ANY of the chunks covering these blocks changed
    db.execute(
        text("""
            UPDATE document_index_states
            SET state = 'stale',
                target_version = :ver,
                updated_at = now()
            WHERE document_id = CAST(:did AS uuid)
              AND state IN ('ready', 'pending')
              AND backend_type IN ('fulltext', 'vector')
        """),
        {"did": str(document_id), "ver": target_version},
    )
    # Graph: also stale (entity relations may have changed)
    db.execute(
        text("""
            UPDATE document_index_states
            SET state = 'stale',
                target_version = :ver,
                updated_at = now()
            WHERE document_id = CAST(:did AS uuid)
              AND state IN ('ready', 'pending')
              AND backend_type = 'graph'
        """),
        {"did": str(document_id), "ver": target_version},
    )


def get_index_states(db: Session, document_id: UUID) -> list[dict]:
    rows = db.execute(
        text("""
            SELECT backend_type, state, indexed_version, target_version,
                   last_error, error_count, built_at, created_at, updated_at
            FROM document_index_states
            WHERE document_id = CAST(:did AS uuid)
            ORDER BY backend_type
        """),
        {"did": document_id},
    ).mappings().all()
    return [dict(r) for r in rows]


def rebuild_stale_for_project(
    db: Session, project_id: UUID, backends: list[str] | None = None
) -> int:
    """Mark stale documents in a project for rebuild. Returns count."""
    if backends:
        result = db.execute(
            text("""
                UPDATE document_index_states dis
                SET state = 'building', updated_at = now()
                FROM knowledge_documents kd
                WHERE dis.document_id = kd.document_id
                  AND kd.project_id = :pid
                  AND dis.state = 'stale'
                  AND dis.backend_type = ANY(:btypes)
            """),
            {"pid": project_id, "btypes": backends},
        )
    else:
        result = db.execute(
            text("""
                UPDATE document_index_states dis
                SET state = 'building', updated_at = now()
                FROM knowledge_documents kd
                WHERE dis.document_id = kd.document_id
                  AND kd.project_id = :pid
                  AND dis.state = 'stale'
            """),
            {"pid": project_id},
        )
    return result.rowcount
