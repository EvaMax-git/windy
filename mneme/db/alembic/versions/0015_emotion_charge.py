"""Add emotion_charge and uncertainty_score columns to memories table.

emotion_charge is inferred from behavioral signals (memory_text content,
reinforcement patterns, access frequency) — NOT manually annotated.
It interacts with the decay engine: different emotions decay at different rates.

uncertainty_score tracks how uncertain the inference engine is about its
emotion_charge classification (0.0 = high confidence, 1.0 = pure guess).

Revision ID: 0015_emotion_charge
Revises: 0014_memory_decay_score
Create Date: 2026-05-07
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0015_emotion_charge"
down_revision: str | Sequence[str] | None = "e6125744df27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── memories: emotion columns ─────────────────────────────────────────
    op.add_column(
        "memories",
        sa.Column(
            "emotion_charge",
            sa.String(24),
            nullable=False,
            server_default="neutral",
        ),
    )
    op.add_column(
        "memories",
        sa.Column(
            "uncertainty_score",
            sa.Numeric(5, 4),
            nullable=False,
            server_default="0.5",
        ),
    )
    op.add_column(
        "memories",
        sa.Column(
            "last_emotion_inferred_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )

    # CHECK constraint for emotion_charge
    op.create_check_constraint(
        "memories_emotion_charge_check",
        "memories",
        "emotion_charge IN ('neutral', 'embarrassed', 'proud', 'fearful')",
    )

    # Extend memory_versions.action to include 'emotion_infer'
    op.drop_constraint(
        "memory_versions_action_check",
        "memory_versions",
        type_="check",
    )
    op.create_check_constraint(
        "memory_versions_action_check",
        "memory_versions",
        "action IN ('create','update','merge','expire','delete',"
        "'restore','refine','dedup','quality','decay','reinforce','emotion_infer')",
    )

    # Index for emotion sweeper queries
    op.create_index(
        "idx_memories_emotion_charge",
        "memories",
        ["emotion_charge", "uncertainty_score", "last_emotion_inferred_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_memories_emotion_charge", table_name="memories")

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
        "'restore','refine','dedup','quality','decay','reinforce')",
    )

    op.drop_constraint("memories_emotion_charge_check", "memories", type_="check")

    op.drop_column("memories", "last_emotion_inferred_at")
    op.drop_column("memories", "uncertainty_score")
    op.drop_column("memories", "emotion_charge")
