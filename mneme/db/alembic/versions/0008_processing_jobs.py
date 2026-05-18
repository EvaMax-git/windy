"""processing_jobs table — knowledge import job state tracker

Revision ID: 0008_processing_jobs
Revises: 0007_sub_library_registry
Create Date: 2026-05-06
"""

from collections.abc import Sequence

from alembic import op


revision: str = "0008_processing_jobs"
down_revision: str | Sequence[str] | None = "0007_sub_library_registry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE processing_jobs (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            asset_id uuid NOT NULL,
            pipeline_id uuid NOT NULL,
            target_stores text[] NOT NULL DEFAULT '{}',
            status varchar(32) NOT NULL DEFAULT 'queued',
            chunks_produced int NOT NULL DEFAULT 0,
            error text,
            started_at timestamptz,
            completed_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            CHECK (status IN ('queued', 'processing', 'done', 'failed'))
        );
    """)

    # Index for polling by status
    op.execute("""
        CREATE INDEX ix_processing_jobs_status ON processing_jobs (status);
    """)

    # Index for looking up jobs by asset
    op.execute("""
        CREATE INDEX ix_processing_jobs_asset_id ON processing_jobs (asset_id);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS processing_jobs;")
