"""Memory index lifecycle manager (P4-07).

Hooks called from ``mneme/db/memories.py`` after memory lifecycle events:
* ``on_memory_activated`` — create ``memory_index_entries`` row (fts_state='ready').
* ``on_memory_updated``   — mark old entries stale, create new entry.
"""

from __future__ import annotations

import hashlib
from uuid import UUID

from sqlalchemy.orm import Session

from mneme.db.memory_index_entries import (
    create_index_entry,
    mark_entries_stale,
)


def _compute_content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _build_index_text(title: str | None, memory_text: str) -> str:
    t = (title or "").strip()
    m = memory_text.strip()
    return f"{t} {m}" if t else m


def on_memory_activated(
    db: Session,
    *,
    memory_id: UUID,
    version: int,
    project_id: UUID,
    title: str | None,
    memory_text: str,
) -> None:
    """Create the initial ``memory_index_entries`` row.

    ``fts_state='ready'`` — ``fts_vector`` is GENERATED ALWAYS STORED.
    ``vector_state='pending'`` — Phase 4 leaves embedding to Phase 5+.
    """
    index_text = _build_index_text(title, memory_text)
    content_hash = _compute_content_hash(memory_text)

    create_index_entry(
        db,
        memory_id=memory_id,
        memory_version=version,
        project_id=project_id,
        index_text=index_text,
        content_hash=content_hash,
        fts_state="ready",
    )


def on_memory_expired(
    db: Session,
    *,
    memory_id: UUID,
) -> None:
    """Mark all index entries for this memory as ``fts_state='stale'``.

    Called from ``_transition_status`` when a memory is expired.
    """
    mark_entries_stale(db, memory_id=memory_id)


def on_memory_restored(
    db: Session,
    *,
    memory_id: UUID,
    version: int,
    project_id: UUID,
    title: str | None,
    memory_text: str,
) -> None:
    """Restore index entries after a memory is restored from expired/deleted.

    Creates a fresh entry with ``fts_state='ready'`` so FTS search can find
    the restored memory again.
    """
    index_text = _build_index_text(title, memory_text)
    content_hash = _compute_content_hash(memory_text)
    create_index_entry(
        db,
        memory_id=memory_id,
        memory_version=version,
        project_id=project_id,
        index_text=index_text,
        content_hash=content_hash,
        fts_state="ready",
    )


def on_memory_deleted(
    db: Session,
    *,
    memory_id: UUID,
) -> None:
    """Mark all index entries for this memory as ``fts_state='stale'``.

    Called from ``_transition_status`` when a memory is soft-deleted.
    """
    mark_entries_stale(db, memory_id=memory_id)


def on_memory_updated(
    db: Session,
    *,
    memory_id: UUID,
    old_version: int,
    new_version: int,
    project_id: UUID,
    title: str | None,
    memory_text: str,
) -> None:
    """Handle index after memory content update.

    1. Mark all existing entries for this memory as ``fts_state='stale'``.
    2. Create a new entry for the new version with ``fts_state='ready'``.
    """
    mark_entries_stale(db, memory_id=memory_id)

    index_text = _build_index_text(title, memory_text)
    content_hash = _compute_content_hash(memory_text)

    create_index_entry(
        db,
        memory_id=memory_id,
        memory_version=new_version,
        project_id=project_id,
        index_text=index_text,
        content_hash=content_hash,
        fts_state="ready",
    )
