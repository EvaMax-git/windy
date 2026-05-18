"""sub_library_registry table

Revision ID: 0007_sub_library_registry
Revises: 0006_pipeline_registry
Create Date: 2026-05-06
"""

from collections.abc import Sequence

from alembic import op


revision: str = "0007_sub_library_registry"
down_revision: str | Sequence[str] | None = "0006_pipeline_registry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE sub_library_registry (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            name varchar(200) NOT NULL,
            type varchar(32) NOT NULL,
            capability_json jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            CHECK (type IN ('vector', 'graph', 'fulltext', 'custom'))
        );
    """)

    # Seed the three default sub-libraries
    op.execute("""
        INSERT INTO sub_library_registry (id, name, type, capability_json)
        VALUES
            (gen_random_uuid(), '默认向量库',      'vector',   '{"accept_chunks": true,  "search": true,  "normalize": true}'::jsonb),
            (gen_random_uuid(), '默认图谱库',      'graph',    '{"accept_chunks": false, "search": true,  "normalize": false}'::jsonb),
            (gen_random_uuid(), '默认全文索引',    'fulltext', '{"accept_chunks": true,  "search": true,  "normalize": true}'::jsonb)
        ;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS sub_library_registry;")
