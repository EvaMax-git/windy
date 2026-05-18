"""pipeline_registry table — knowledge pipeline processor registration

Revision ID: 0006_pipeline_registry
Revises: 0005_memory_stores
Create Date: 2026-05-06
"""

from collections.abc import Sequence

from alembic import op


revision: str = "0006_pipeline_registry"
down_revision: str | Sequence[str] | None = "0005_memory_stores"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Create pipeline_registry table
    op.execute("""
        CREATE TABLE pipeline_registry (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            name varchar(200) NOT NULL,
            input_formats text[] NOT NULL DEFAULT '{}',
            processor_module varchar(200) NOT NULL,
            accept_chunk_types text[] NOT NULL DEFAULT '{}',
            target_stores text[] NOT NULL DEFAULT '{}',
            created_at timestamptz NOT NULL DEFAULT now()
        );
    """)

    # Create unique index on processor_module for fast lookup
    op.execute("""
        CREATE UNIQUE INDEX ix_pipeline_registry_processor_module
        ON pipeline_registry (processor_module);
    """)

    # Insert three seed pipeline registries
    op.execute("""
        INSERT INTO pipeline_registry (name, input_formats, processor_module, accept_chunk_types, target_stores)
        VALUES
        (
            '标准分块',
            ARRAY['text/plain', 'text/markdown', 'text/html', 'application/pdf'],
            'standard_chunking',
            ARRAY['text', 'code', 'table'],
            ARRAY['knowledge_store']
        ),
        (
            'OCR',
            ARRAY['image/png', 'image/jpeg', 'image/tiff', 'application/pdf'],
            'ocr',
            ARRAY['text', 'image'],
            ARRAY['knowledge_store']
        ),
        (
            '对话解析',
            ARRAY['application/json', 'text/csv'],
            'conversation_parser',
            ARRAY['conversation', 'message', 'text'],
            ARRAY['memory_store']
        );
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS pipeline_registry;")
