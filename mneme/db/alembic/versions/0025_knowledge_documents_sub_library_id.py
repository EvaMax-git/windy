"""Add sub_library_id column to knowledge_documents

Revision ID: 0025_knowledge_documents_sub_library_id
Revises: 0024_legacy_mapping_registry
Create Date: 2026-05-09
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0025_knowledge_documents_sub_library_id"
down_revision: str | Sequence[str] | None = "0024_legacy_mapping_registry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "knowledge_documents",
        sa.Column("sub_library_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_knowledge_documents_sub_library",
        "knowledge_documents",
        "sub_library_registry",
        ["sub_library_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_knowledge_documents_sub_library", "knowledge_documents", type_="foreignkey")
    op.drop_column("knowledge_documents", "sub_library_id")
