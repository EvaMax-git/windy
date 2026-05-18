"""Agent Cards system — identity / soul / tool / user_profile

Revision ID: 0012_agent_cards
Revises: 0011_ingest_metadata
Create Date: 2026-05-07
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0012_agent_cards"
down_revision: str = "0009_merge_0006_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── agent_cards (identity/soul/tool/user_profile) ──
    op.create_table(
        "agent_cards",
        sa.Column("card_id", sa.dialects.postgresql.UUID(as_uuid=True),
                  primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("agent_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("card_type", sa.String(24), nullable=False,
                  comment="identity | soul | tool | user_profile"),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("content_json", sa.dialects.postgresql.JSONB,
                  nullable=False, server_default=sa.text("'{}'::jsonb"),
                  comment="Card-specific content/payload"),
        sa.Column("status", sa.String(24), nullable=False,
                  server_default=sa.text("'active'")),
        sa.Column("display_order", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )

    op.create_index("ix_agent_cards_agent_id", "agent_cards", ["agent_id"])
    op.create_index("ix_agent_cards_card_type", "agent_cards", ["card_type"])
    op.create_check_constraint(
        "ck_agent_cards_card_type",
        "agent_cards",
        "card_type IN ('identity', 'soul', 'tool', 'user_profile')",
    )
    op.create_check_constraint(
        "ck_agent_cards_status",
        "agent_cards",
        "status IN ('active', 'disabled', 'archived')",
    )

    # ── agent_tool_items (detail layer for tool cards) ──
    op.create_table(
        "agent_tool_items",
        sa.Column("item_id", sa.dialects.postgresql.UUID(as_uuid=True),
                  primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("card_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("tool_type", sa.String(64), nullable=True,
                  comment="e.g. api | function | script | builtin | mcp"),
        sa.Column("config_json", sa.dialects.postgresql.JSONB,
                  nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("input_schema", sa.dialects.postgresql.JSONB, nullable=True,
                  comment="JSON Schema for tool input parameters"),
        sa.Column("output_schema", sa.dialects.postgresql.JSONB, nullable=True,
                  comment="JSON Schema for tool output"),
        sa.Column("status", sa.String(24), nullable=False,
                  server_default=sa.text("'active'")),
        sa.Column("display_order", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )

    op.create_index("ix_agent_tool_items_card_id", "agent_tool_items", ["card_id"])
    op.create_check_constraint(
        "ck_agent_tool_items_status",
        "agent_tool_items",
        "status IN ('active', 'disabled', 'archived')",
    )
    op.create_foreign_key(
        "fk_agent_tool_items_card_id",
        "agent_tool_items", "agent_cards",
        ["card_id"], ["card_id"],
        ondelete="CASCADE",
    )

    # ── Updated-at triggers ──
    for table in ("agent_cards", "agent_tool_items"):
        op.execute(f"""
            CREATE TRIGGER trg_{table}_updated_at
                BEFORE UPDATE ON {table}
                FOR EACH ROW EXECUTE FUNCTION set_updated_at()
        """)


def downgrade() -> None:
    for table in ("agent_tool_items", "agent_cards"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_updated_at ON {table}")
    op.drop_table("agent_tool_items")
    op.drop_table("agent_cards")
