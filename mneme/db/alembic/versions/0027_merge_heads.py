"""Merge heads 0025 and 0026

Revision ID: 0027
Revises: 0025_knowledge_documents_sub_library_id, 0026
Create Date: 2026-05-17
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0027"
down_revision: str | Sequence[str] | None = ("0025_knowledge_documents_sub_library_id", "0026")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
