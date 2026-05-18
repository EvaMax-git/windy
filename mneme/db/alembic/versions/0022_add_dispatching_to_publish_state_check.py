"""add dispatching to events publish_state check constraint

Revision ID: 0022
Revises: 0021
Create Date: 2026-05-08
"""
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

OLD_STATES = (
    "'pending', 'dispatched', 'delivered', 'failed', 'dead_letter'"
)
NEW_STATES = (
    "'pending', 'dispatching', 'dispatched', 'delivered', 'failed', 'dead_letter'"
)


def upgrade() -> None:
    op.drop_constraint("events_publish_state_check", "events", type_="check")
    op.create_check_constraint(
        "events_publish_state_check",
        "events",
        f"publish_state IN ({NEW_STATES})",
    )


def downgrade() -> None:
    op.drop_constraint("events_publish_state_check", "events", type_="check")
    op.create_check_constraint(
        "events_publish_state_check",
        "events",
        f"publish_state IN ({OLD_STATES})",
    )