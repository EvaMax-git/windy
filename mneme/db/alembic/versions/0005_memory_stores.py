"""memory_stores table + agents.store_id + memories.store_id columns

Revision ID: 0005_memory_stores
Revises: 0004_p7_eval_tables
Create Date: 2026-05-05
"""

from collections.abc import Sequence

from alembic import op


revision: str = "0005_memory_stores"
down_revision: str | Sequence[str] | None = "0004_p7_eval_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Create memory_stores table
    op.execute("""
        CREATE TABLE memory_stores (
            store_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            agent_id uuid REFERENCES agents(agent_id) ON DELETE SET NULL,
            name varchar(200) NOT NULL,
            type varchar(32) NOT NULL,
            description text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CHECK (type IN ('memory_card', 'identity', 'skill', 'rule', 'tool'))
        );
    """)

    # Add store_id column to agents table
    op.execute("""
        ALTER TABLE agents
        ADD COLUMN store_id uuid REFERENCES memory_stores(store_id) ON DELETE SET NULL;
    """)

    # Add store_id column to memories table for store-based isolation
    op.execute("""
        ALTER TABLE memories
        ADD COLUMN store_id uuid REFERENCES memory_stores(store_id) ON DELETE SET NULL;
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE memories DROP COLUMN IF EXISTS store_id;")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS store_id;")
    op.execute("DROP TABLE IF EXISTS memory_stores;")
