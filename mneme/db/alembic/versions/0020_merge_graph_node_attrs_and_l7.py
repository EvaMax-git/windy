"""Merge 0015_graph_node_attrs + 0019_l7_event_sourcing heads.

Resolves the two Alembic heads by merging the orphaned
0015_graph_node_attrs branch (off 0014_memory_decay_score) with the
main chain (e6125744df27 → 0015_emotion_charge → ... → 0019).

Since both 0015_graph_node_attrs and 0015_emotion_charge independently
recreated the ``memory_versions_action_check`` constraint (adding
``graph_link`` and ``emotion_infer`` respectively), the merge step
recreates it with BOTH values to ensure neither is lost.

Revision ID: 0020_merge_graph_node_attrs_and_l7
Revises: 0015_graph_node_attrs, 0019_l7_event_sourcing
Create Date: 2026-05-08
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0020_merge_graph_node_attrs_and_l7"
down_revision: str | Sequence[str] | None = (
    "0015_graph_node_attrs",
    "0019_l7_event_sourcing",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── memory_versions.action: ensure BOTH graph_link AND emotion_infer ──
    op.drop_constraint(
        "memory_versions_action_check",
        "memory_versions",
        type_="check",
    )
    op.create_check_constraint(
        "memory_versions_action_check",
        "memory_versions",
        "action IN ('create','update','merge','expire','delete',"
        "'restore','refine','dedup','quality','decay','reinforce',"
        "'graph_link','emotion_infer')",
    )


def downgrade() -> None:
    # Revert to the post-0015_emotion_charge constraint (loses graph_link)
    op.drop_constraint(
        "memory_versions_action_check",
        "memory_versions",
        type_="check",
    )
    op.create_check_constraint(
        "memory_versions_action_check",
        "memory_versions",
        "action IN ('create','update','merge','expire','delete',"
        "'restore','refine','dedup','quality','decay','reinforce',"
        "'emotion_infer')",
    )
