"""Add model_id column to agents table

Revision ID: 0023_agent_model_id
Revises: 0022_add_dispatching_to_publish_state_check
Create Date: 2026-05-08
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0023_agent_model_id"
down_revision: str = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("model_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_agents_model_id",
        "agents",
        "provider_models",
        ["model_id"],
        ["provider_model_id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_agents_model_id", "agents", type_="foreignkey")
    op.drop_column("agents", "model_id")
