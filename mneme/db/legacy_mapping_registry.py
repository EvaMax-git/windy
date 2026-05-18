"""DAL for legacy_mapping_registry — import source-to-target deduplication."""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session


_INSERT_MAPPING = text("""
    INSERT INTO legacy_mapping_registry (
        mapping_id, import_run_id, source_table, source_pk, target_type, target_id
    ) VALUES (
        :mapping_id, :import_run_id, :source_table, :source_pk, :target_type, :target_id
    )
    ON CONFLICT (import_run_id, source_table, source_pk) DO NOTHING
    RETURNING mapping_id
""")

_LOOKUP_TARGET = text("""
    SELECT target_type, target_id
    FROM legacy_mapping_registry
    WHERE import_run_id = :import_run_id
      AND source_table = :source_table
      AND source_pk = :source_pk
    LIMIT 1
""")


def register_mapping(
    db: Session,
    *,
    import_run_id: UUID,
    source_table: str,
    source_pk: str,
    target_type: str,
    target_id: UUID,
) -> UUID | None:
    """Register a source-to-target mapping. Returns mapping_id or None if duplicate."""
    mapping_id = uuid4()
    row = db.execute(
        _INSERT_MAPPING,
        {
            "mapping_id": mapping_id,
            "import_run_id": import_run_id,
            "source_table": source_table,
            "source_pk": source_pk,
            "target_type": target_type,
            "target_id": target_id,
        },
    ).fetchone()
    if row is None:
        return None
    return mapping_id


def lookup_target(
    db: Session,
    import_run_id: UUID,
    source_table: str,
    source_pk: str,
) -> tuple[str, UUID] | None:
    """Look up a previously-imported target by source identity."""
    row = db.execute(
        _LOOKUP_TARGET,
        {
            "import_run_id": import_run_id,
            "source_table": source_table,
            "source_pk": source_pk,
        },
    ).fetchone()
    if row is None:
        return None
    return (row._mapping["target_type"], row._mapping["target_id"])
