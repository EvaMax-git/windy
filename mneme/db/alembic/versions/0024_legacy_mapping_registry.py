"""Create legacy_mapping_registry table for import deduplication

Revision ID: 0024_legacy_mapping_registry
Revises: 0023_agent_model_id
Create Date: 2026-05-08
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0024_legacy_mapping_registry"
down_revision: str = "0023_agent_model_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "legacy_mapping_registry",
        sa.Column("mapping_id", sa.dialects.postgresql.UUID(as_uuid=True),
                  primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("import_run_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_table", sa.String(128), nullable=False),
        sa.Column("source_pk", sa.String(512), nullable=False),
        sa.Column("target_type", sa.String(64), nullable=False),
        sa.Column("target_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("mapped_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.UniqueConstraint("import_run_id", "source_table", "source_pk",
                            name="uq_legacy_mapping_source"),
    )
    op.create_index("idx_legacy_mapping_target", "legacy_mapping_registry",
                    ["target_type", "target_id"])


def downgrade() -> None:
    op.drop_index("idx_legacy_mapping_target", table_name="legacy_mapping_registry")
    op.drop_table("legacy_mapping_registry")
