"""Add name column to agent_tokens

Revision ID: 0010_add_agent_token_name
Revises: 0009_merge_0006_0007
Create Date: 2026-05-07
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0010_add_agent_token_name"
down_revision: str = "0009_merge_0006_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_tokens",
        sa.Column("name", sa.String(128), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_tokens", "name")
