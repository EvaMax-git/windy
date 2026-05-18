"""Add decay_score and decay_state columns to memories table.

Time-decay mechanism: each active memory has a decay_score that decreases
over time. Decay state thresholds determine action: active > decaying > silent > archived.

Revision ID: 0014_memory_decay_score
Revises: 0013_card_store_types
Create Date: 2026-05-07
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0014_memory_decay_score"
down_revision: str = "0012_agent_cards"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── memories: decay columns ─────────────────────────────────────────
    op.add_column(
        "memories",
        sa.Column(
            "decay_score",
            sa.Numeric(5, 4),
            nullable=False,
            server_default="1.0",
        ),
    )
    op.add_column(
        "memories",
        sa.Column(
            "decay_state",
            sa.String(24),
            nullable=False,
            server_default="active",
        ),
    )
    op.add_column(
        "memories",
        sa.Column("last_decayed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "memories",
        sa.Column("last_reinforced_at", sa.DateTime(timezone=True), nullable=True),
    )

    # CHECK constraint for decay_state
    op.create_check_constraint(
        "memories_decay_state_check",
        "memories",
        "decay_state IN ('active', 'decaying', 'silent', 'archived')",
    )

    # Extend memory_versions.action to include 'decay' and 'reinforce'
    op.drop_constraint(
        "memory_versions_action_check",
        "memory_versions",
        type_="check",
    )
    op.create_check_constraint(
        "memory_versions_action_check",
        "memory_versions",
        "action IN ('create','update','merge','expire','delete',"
        "'restore','refine','dedup','quality','decay','reinforce')",
    )

    # Index for decay sweeper queries
    op.create_index(
        "idx_memories_decay_state_score",
        "memories",
        ["decay_state", "decay_score", "last_decayed_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_memories_decay_state_score", table_name="memories")

    # Revert memory_versions.action CHECK
    op.drop_constraint(
        "memory_versions_action_check",
        "memory_versions",
        type_="check",
    )
    op.create_check_constraint(
        "memory_versions_action_check",
        "memory_versions",
        "action IN ('create','update','merge','expire','delete',"
        "'restore','refine','dedup','quality')",
    )

    op.drop_constraint("memories_decay_state_check", "memories", type_="check")

    op.drop_column("memories", "last_reinforced_at")
    op.drop_column("memories", "last_decayed_at")
    op.drop_column("memories", "decay_state")
    op.drop_column("memories", "decay_score")
