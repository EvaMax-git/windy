"""Add neg_space_events + trust_accounts tables.

neg_space_events — tracks when the AI avoids topics, deletes sentences, or
remains silent (负空间记录). Each event records what was avoided/removed,
the reason, and surrounding context.

trust_accounts — per-subject trust ledger tracking call counts, success rate,
and user feedback (信任账户). Used to compute a composite trust_score.

Revision ID: 0016_neg_space_and_trust
Revises: 0015_emotion_charge
Create Date: 2026-05-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016_neg_space_and_trust"
down_revision: str | Sequence[str] | None = "0015_emotion_charge"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── neg_space_events ─────────────────────────────────────────────────────
    op.create_table(
        "neg_space_events",
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "event_category",
            sa.String(32),
            nullable=False,
        ),
        sa.Column(
            "event_type",
            sa.String(64),
            nullable=False,
        ),
        sa.Column("trigger_text", sa.Text, nullable=True),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column(
            "severity",
            sa.String(16),
            nullable=False,
            server_default="medium",
        ),
        sa.Column(
            "context_json",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "event_category IN ('topic_avoided', 'sentence_deleted', 'silence', 'refusal', 'redirection', 'other')",
            name="neg_space_events_event_category_check",
        ),
        sa.CheckConstraint(
            "severity IN ('low', 'medium', 'high', 'critical')",
            name="neg_space_events_severity_check",
        ),
    )

    op.create_index(
        "idx_neg_space_events_agent",
        "neg_space_events",
        ["agent_id", "created_at"],
    )
    op.create_index(
        "idx_neg_space_events_conversation",
        "neg_space_events",
        ["conversation_id"],
    )
    op.create_index(
        "idx_neg_space_events_category",
        "neg_space_events",
        ["event_category", "severity"],
    )

    # ── trust_accounts ───────────────────────────────────────────────────────
    op.create_table(
        "trust_accounts",
        sa.Column(
            "trust_account_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "subject_type",
            sa.String(24),
            nullable=False,
        ),
        sa.Column("subject_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "capability_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "total_calls",
            sa.Integer,
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "successful_calls",
            sa.Integer,
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "failed_calls",
            sa.Integer,
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "success_rate",
            sa.Numeric(5, 4),
            nullable=False,
            server_default="0.0000",
        ),
        sa.Column(
            "positive_feedback",
            sa.Integer,
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "negative_feedback",
            sa.Integer,
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "neutral_feedback",
            sa.Integer,
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "trust_score",
            sa.Numeric(5, 4),
            nullable=False,
            server_default="0.5000",
        ),
        sa.Column(
            "last_evaluated_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "metadata_json",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "subject_type IN ('agent', 'user', 'service', 'system')",
            name="trust_accounts_subject_type_check",
        ),
        sa.CheckConstraint(
            "success_rate >= 0 AND success_rate <= 1",
            name="trust_accounts_success_rate_check",
        ),
        sa.CheckConstraint(
            "trust_score >= 0 AND trust_score <= 1",
            name="trust_accounts_trust_score_check",
        ),
        sa.UniqueConstraint(
            "subject_type",
            "subject_id",
            "capability_id",
            name="uq_trust_accounts_subject_capability",
        ),
    )

    op.create_index(
        "idx_trust_accounts_subject",
        "trust_accounts",
        ["subject_type", "subject_id"],
    )
    op.create_index(
        "idx_trust_accounts_score",
        "trust_accounts",
        ["trust_score", "success_rate"],
    )


def downgrade() -> None:
    op.drop_index("idx_trust_accounts_score", table_name="trust_accounts")
    op.drop_index("idx_trust_accounts_subject", table_name="trust_accounts")
    op.drop_table("trust_accounts")

    op.drop_index("idx_neg_space_events_category", table_name="neg_space_events")
    op.drop_index("idx_neg_space_events_conversation", table_name="neg_space_events")
    op.drop_index("idx_neg_space_events_agent", table_name="neg_space_events")
    op.drop_table("neg_space_events")
