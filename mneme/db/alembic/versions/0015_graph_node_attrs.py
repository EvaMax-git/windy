"""Add node_type to memories + extend memory_relations.relation_type CHECK.

Revision ID: 0015_graph_node_attrs
Revises: 0014_memory_decay_score
Create Date: 2026-05-07

- memories.node_type: VARCHAR(24) with CHECK (episode, fact, reflection, concept)
- memory_relations.relation_type: extend to include similar, causal, temporal,
  contradicts, references
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_graph_node_attrs"
down_revision: str | Sequence[str] | None = "0014_memory_decay_score"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── memories: node_type column ───────────────────────────────────────
    op.add_column(
        "memories",
        sa.Column("node_type", sa.String(24), nullable=True),
    )
    op.create_check_constraint(
        "memories_node_type_check",
        "memories",
        "node_type IS NULL OR node_type IN ('episode', 'fact', 'reflection', 'concept')",
    )

    # ── memory_relations: extend relation_type CHECK ────────────────────
    op.drop_constraint(
        "memory_relations_relation_type_check",
        "memory_relations",
        type_="check",
    )
    op.create_check_constraint(
        "memory_relations_relation_type_check",
        "memory_relations",
        "relation_type IN ('conflicts_with','supersedes','merged_into','duplicates',"
        "'supports','similar','causal','temporal','contradicts','references')",
    )

    # ── memory_versions.action: add 'graph_link' action ─────────────────
    op.drop_constraint(
        "memory_versions_action_check",
        "memory_versions",
        type_="check",
    )
    op.create_check_constraint(
        "memory_versions_action_check",
        "memory_versions",
        "action IN ('create','update','merge','expire','delete',"
        "'restore','refine','dedup','quality','decay','reinforce','graph_link')",
    )

    # ── index for node_type filtering ────────────────────────────────────
    op.create_index(
        "idx_memories_node_type",
        "memories",
        ["node_type", "project_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_memories_node_type", table_name="memories")

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

    # Revert memory_relations.relation_type CHECK
    op.drop_constraint(
        "memory_relations_relation_type_check",
        "memory_relations",
        type_="check",
    )
    op.create_check_constraint(
        "memory_relations_relation_type_check",
        "memory_relations",
        "relation_type IN ('conflicts_with','supersedes','merged_into','duplicates','supports')",
    )

    # Drop node_type column
    op.drop_constraint("memories_node_type_check", "memories", type_="check")
    op.drop_column("memories", "node_type")
