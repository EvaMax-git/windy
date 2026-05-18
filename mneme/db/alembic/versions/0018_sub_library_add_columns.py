"""Add key + metadata_json columns to sub_library_registry, set key on defaults.

Revision ID: 0018_sub_library_add_columns
Revises: 0017_eval_ab_tables
Create Date: 2026-05-07

The original migration 0007 created the table without ``key`` and
``metadata_json`` columns, but the CRUD layer in sub_library_registry.py
references them.  This migration:

1. Adds ``key`` (varchar, NOT NULL default '') and
   ``metadata_json`` (jsonb, NOT NULL default '{}') columns.
2. Populates ``key`` on the three default seed entries so that the frontend
   can distinguish them (vector -> 'vector', graph -> 'graph', fulltext -> 'fulltext').
"""

from collections.abc import Sequence

from alembic import op


revision: str = "0018_sub_library_add_columns"
down_revision: str | Sequence[str] | None = "0017_eval_ab_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE sub_library_registry
            ADD COLUMN IF NOT EXISTS key varchar(64) NOT NULL DEFAULT '',
            ADD COLUMN IF NOT EXISTS metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb;
    """)
    # Seed key values for the default libraries inserted by migration 0007
    op.execute("UPDATE sub_library_registry SET key = 'vector'   WHERE type = 'vector'   AND key = '';")
    op.execute("UPDATE sub_library_registry SET key = 'graph'    WHERE type = 'graph'    AND key = '';")
    op.execute("UPDATE sub_library_registry SET key = 'fulltext' WHERE type = 'fulltext' AND key = '';")


def downgrade() -> None:
    op.execute("ALTER TABLE sub_library_registry DROP COLUMN IF EXISTS metadata_json;")
    op.execute("ALTER TABLE sub_library_registry DROP COLUMN IF EXISTS key;")
