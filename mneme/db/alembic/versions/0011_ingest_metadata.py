"""Add metadata_json to pipeline_registry and sub_library_registry

Add key column to sub_library_registry for frontend-friendly identifier.

Revision ID: 0011_ingest_metadata
Revises: 0010_add_agent_token_name
Create Date: 2026-05-07
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0011_ingest_metadata"
down_revision: str = "0010_add_agent_token_name"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Add metadata_json to pipeline_registry
    op.add_column(
        "pipeline_registry",
        sa.Column("metadata_json", sa.dialects.postgresql.JSONB, nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
    )

    # Add key + metadata_json to sub_library_registry
    op.add_column(
        "sub_library_registry",
        sa.Column("key", sa.String(100), nullable=True),
    )
    op.add_column(
        "sub_library_registry",
        sa.Column("metadata_json", sa.dialects.postgresql.JSONB, nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
    )
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS ix_sub_library_registry_key
        ON sub_library_registry (key)
        WHERE key IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_sub_library_registry_key")
    op.drop_column("sub_library_registry", "metadata_json")
    op.drop_column("sub_library_registry", "key")
    op.drop_column("pipeline_registry", "metadata_json")
