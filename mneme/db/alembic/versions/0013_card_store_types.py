"""Extend memory_stores.type CHECK constraint with card types.

Add soul_card, identity_card, tool_catalog, user_profile, tool_detail
to support the context assembly engine's card-based injection strategy.

Revision ID: 0013_card_store_types
Revises: 0012_agent_cards
Create Date: 2026-05-07
"""

from collections.abc import Sequence

from alembic import op


revision: str = "0013_card_store_types"
down_revision: str | Sequence[str] | None = "0012_agent_cards"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop old CHECK constraint and add new one with extended type list."""
    op.execute("""
        ALTER TABLE memory_stores
        DROP CONSTRAINT IF EXISTS memory_stores_type_check;
    """)
    op.execute("""
        ALTER TABLE memory_stores
        ADD CONSTRAINT memory_stores_type_check
        CHECK (type IN (
            'memory_card', 'identity', 'skill', 'rule', 'tool',
            'soul_card', 'identity_card', 'tool_catalog',
            'user_profile', 'tool_detail'
        ));
    """)


def downgrade() -> None:
    """Restore original CHECK constraint without new card types."""
    op.execute("""
        ALTER TABLE memory_stores
        DROP CONSTRAINT IF EXISTS memory_stores_type_check;
    """)
    op.execute("""
        ALTER TABLE memory_stores
        ADD CONSTRAINT memory_stores_type_check
        CHECK (type IN ('memory_card', 'identity', 'skill', 'rule', 'tool'));
    """)
