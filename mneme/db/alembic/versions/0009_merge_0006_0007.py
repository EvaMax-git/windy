"""Merge migration — follows 0008_processing_jobs, completing the sub_library+processing chain.

Revision ID: 0009_merge_0006_0007
Revises: 0008_processing_jobs
Create Date: 2026-05-06
"""

from collections.abc import Sequence

# No schema operations needed — all tables already created by prior revisions.
revision: str = "0009_merge_0006_0007"
down_revision: str | Sequence[str] | None = "0008_processing_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
