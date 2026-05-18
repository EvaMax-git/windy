"""P6 Refine: add quality columns + extend memory_versions CHECK.

Revision ID: 0002_p6_refine_columns
Revises: 0001_baseline_45_tables
Create Date: 2026-05-05

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_p6_refine_columns"
down_revision: str | Sequence[str] | None = "0001_baseline_45_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── memories: 3 new columns ──────────────────────────────────────────
    op.add_column(
        "memories",
        sa.Column("quality_score", sa.Numeric(5, 4), nullable=True),
    )
    op.add_column(
        "memories",
        sa.Column(
            "search_weight",
            sa.Numeric(5, 4),
            nullable=False,
            server_default="1.0",
        ),
    )
    op.add_column(
        "memories",
        sa.Column("last_refined_at", sa.DateTime(timezone=True), nullable=True),
    )

    # ── memory_index_entries: 2 new columns ──────────────────────────────
    op.add_column(
        "memory_index_entries",
        sa.Column("quality_score", sa.Numeric(5, 4), nullable=True),
    )
    op.add_column(
        "memory_index_entries",
        sa.Column(
            "search_weight",
            sa.Numeric(5, 4),
            nullable=False,
            server_default="1.0",
        ),
    )

    # ── memory_versions.action CHECK expansion ───────────────────────────
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


def downgrade() -> None:
    # ── Revert CHECK to original 6 values ────────────────────────────────
    op.drop_constraint(
        "memory_versions_action_check",
        "memory_versions",
        type_="check",
    )
    op.create_check_constraint(
        "memory_versions_action_check",
        "memory_versions",
        "action IN ('create','update','merge','expire','delete','restore')",
    )

    # ── Drop columns in reverse order ────────────────────────────────────
    op.drop_column("memory_index_entries", "search_weight")
    op.drop_column("memory_index_entries", "quality_score")
    op.drop_column("memories", "last_refined_at")
    op.drop_column("memories", "search_weight")
    op.drop_column("memories", "quality_score")
