"""merge agent_cards and decay branches

Revision ID: e6125744df27
Revises: 0011_ingest_metadata, 0013_card_store_types, 0014_memory_decay_score
Create Date: 2026-05-07 02:46:03.556119+00:00

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa



revision: str = 'e6125744df27'
down_revision: str | Sequence[str] | None = ('0011_ingest_metadata', '0013_card_store_types', '0014_memory_decay_score')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
