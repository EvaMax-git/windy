"""Create project_backends, pipeline_rules, document_index_states,
backend_type_registry, document_references, document_tags, document_tag_links.
Modify assets and knowledge_documents with new columns and CHECK constraints.
Backfill default backends and pipeline rules for existing projects.

Revision ID: 0026_project_backends_and_related
Revises: 0015_graph_node_attrs, 0017_eval_ab_tables
Create Date: 2026-05-11
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0026"
down_revision: str | Sequence[str] | None = ("0015_graph_node_attrs", "0017_eval_ab_tables")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── 1. New tables ──────────────────────────────────────────────────

    # 1.1 backend_type_registry (system-level catalogue)
    op.create_table(
        "backend_type_registry",
        sa.Column(
            "backend_type",
            sa.String(40),
            primary_key=True,
        ),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("is_builtin", sa.Boolean, nullable=False,
                  server_default=sa.text("false")),
        sa.Column("handler_module", sa.String(200), nullable=True),
        sa.Column("config_schema", sa.dialects.postgresql.JSONB,
                  nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    # Seed built-in backends (use ASCII names to avoid encoding issues)
    op.execute("""
        INSERT INTO backend_type_registry (backend_type, name, description, is_builtin, handler_module)
        VALUES
          ('fulltext', 'Full-Text Search', 'PostgreSQL GIN keyword search', true, 'mneme.knowledge.fts'),
          ('vector',   'Vector Search', 'pgvector semantic similarity search', true, 'mneme.knowledge.vector'),
          ('graph',    'Knowledge Graph', 'NetworkX entity relationship graph', true, 'mneme.graph_engine')
        ON CONFLICT (backend_type) DO NOTHING
    """)

    # 1.2 project_backends (which backends each project has enabled)
    op.create_table(
        "project_backends",
        sa.Column("id",
                  sa.dialects.postgresql.UUID(as_uuid=True),
                  primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("project_id",
                  sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("projects.project_id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("backend_type", sa.String(40), nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False,
                  server_default=sa.text("true")),
        sa.Column("config_json", sa.dialects.postgresql.JSONB,
                  nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_unique_constraint(
        "uq_project_backends_type",
        "project_backends",
        ["project_id", "backend_type"],
    )
    op.create_index(
        "ix_project_backends_project",
        "project_backends",
        ["project_id"],
    )

    # 1.3 project_pipeline_rules (file pattern → pipeline mapping per project)
    op.create_table(
        "project_pipeline_rules",
        sa.Column("id",
                  sa.dialects.postgresql.UUID(as_uuid=True),
                  primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("project_id",
                  sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("projects.project_id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("pattern", sa.String(200), nullable=False),
        sa.Column("pipeline_def_id",
                  sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("pipeline_defs.pipeline_def_id"),
                  nullable=False),
        sa.Column("priority", sa.Integer, nullable=False,
                  server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_unique_constraint(
        "uq_project_pipeline_rules",
        "project_pipeline_rules",
        ["project_id", "pattern"],
    )

    # 1.4 document_index_states (per-document × per-backend index tracking)
    op.create_table(
        "document_index_states",
        sa.Column("state_id",
                  sa.dialects.postgresql.UUID(as_uuid=True),
                  primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("document_id",
                  sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("knowledge_documents.document_id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("backend_type", sa.String(40), nullable=False),
        sa.Column("state", sa.String(24), nullable=False,
                  server_default=sa.text("'pending'")),
        sa.Column("indexed_version", sa.BigInteger, nullable=False,
                  server_default=sa.text("0")),
        sa.Column("target_version", sa.BigInteger, nullable=False,
                  server_default=sa.text("0")),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("error_count", sa.Integer, nullable=False,
                  server_default=sa.text("0")),
        sa.Column("built_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_check_constraint(
        "ck_document_index_states_state",
        "document_index_states",
        "state IN ('pending','building','ready','stale','failed','disabled')",
    )
    op.create_unique_constraint(
        "uq_document_index_states",
        "document_index_states",
        ["document_id", "backend_type"],
    )
    op.create_index(
        "ix_document_index_states_state",
        "document_index_states",
        ["state"],
    )

    # 1.5 document_references (cross-project document links)
    op.create_table(
        "document_references",
        sa.Column("ref_id",
                  sa.dialects.postgresql.UUID(as_uuid=True),
                  primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("source_doc_id",
                  sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("knowledge_documents.document_id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("target_project_id",
                  sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("projects.project_id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("target_doc_id",
                  sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("knowledge_documents.document_id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("target_uri", sa.Text, nullable=False),
        sa.Column("target_hash", sa.String(64), nullable=True),
        sa.Column("span", sa.dialects.postgresql.JSONB, nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )

    # 1.6 document_tags (project-scoped tag catalogue)
    op.create_table(
        "document_tags",
        sa.Column("id",
                  sa.dialects.postgresql.UUID(as_uuid=True),
                  primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("project_id",
                  sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("projects.project_id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_unique_constraint(
        "uq_document_tags_project_name",
        "document_tags",
        ["project_id", "name"],
    )

    # 1.7 document_tag_links (M:N association document ↔ tag)
    op.create_table(
        "document_tag_links",
        sa.Column("document_id",
                  sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("knowledge_documents.document_id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("tag_id",
                  sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("document_tags.id", ondelete="CASCADE"),
                  nullable=False),
        sa.PrimaryKeyConstraint("document_id", "tag_id"),
    )

    # ── 2. Alter existing tables ───────────────────────────────────────

    # 2.1 assets: add columns + narrow CHECK constraint
    op.add_column("assets",
                  sa.Column("relative_path", sa.Text, nullable=True))
    op.add_column("assets",
                  sa.Column("original_pool_ref", sa.Text, nullable=True))
    op.add_column("assets",
                  sa.Column("staging_expires_at", sa.DateTime(timezone=True), nullable=True))

    # Drop old CHECK, re-create without 'deleted'
    op.drop_constraint("assets_status_check", "assets", type_="check")
    op.create_check_constraint(
        "assets_status_check",
        "assets",
        "status IN ('active', 'archived')",
    )

    op.create_index(
        "ix_assets_pool_ref",
        "assets",
        ["original_pool_ref"],
    )
    op.create_index(
        "ix_assets_staging_expires",
        "assets",
        ["staging_expires_at"],
    )

    # 2.2 knowledge_documents: add columns
    op.add_column("knowledge_documents",
                  sa.Column("lang", sa.String(24), nullable=False,
                            server_default=sa.text("'markdown'")))
    op.add_column("knowledge_documents",
                  sa.Column("folder_path", sa.Text, nullable=True))
    op.add_column("knowledge_documents",
                  sa.Column("pipeline_def_id",
                            sa.dialects.postgresql.UUID(as_uuid=True),
                            sa.ForeignKey("pipeline_defs.pipeline_def_id",
                                          ondelete="SET NULL"),
                            nullable=True))
    op.add_column("knowledge_documents",
                  sa.Column("source_asset_id",
                            sa.dialects.postgresql.UUID(as_uuid=True),
                            sa.ForeignKey("assets.asset_id", ondelete="SET NULL"),
                            nullable=True))
    op.add_column("knowledge_documents",
                  sa.Column("content_hash", sa.String(64), nullable=True))

    op.create_index(
        "ix_knowledge_docs_folder",
        "knowledge_documents",
        ["project_id", "folder_path"],
    )

    # 2.3 processing_jobs: add pipeline_def_id for new pipeline system
    op.add_column("processing_jobs",
                  sa.Column("pipeline_def_id",
                            sa.dialects.postgresql.UUID(as_uuid=True),
                            sa.ForeignKey("pipeline_defs.pipeline_def_id",
                                          ondelete="SET NULL"),
                            nullable=True))

    # ── 3. Data migration ─────────────────────────────────────────────

    # For each existing project, create a default fulltext backend
    op.execute("""
        INSERT INTO project_backends (project_id, backend_type, enabled, config_json)
        SELECT pp.project_id, 'fulltext', true, '{}'::jsonb
        FROM projects pp
        WHERE NOT EXISTS (
            SELECT 1 FROM project_backends pb
            WHERE pb.project_id = pp.project_id AND pb.backend_type = 'fulltext'
        )
    """)

    # For each existing project, create a default pipeline rule (* → standard_chunk)
    # Only if a suitable pipeline_def exists (skip projects with no pipeline_defs at all)
    op.execute("""
        INSERT INTO project_pipeline_rules (project_id, pattern, pipeline_def_id, priority)
        SELECT pp.project_id, '*', chosen.def_id, 0
        FROM projects pp
        CROSS JOIN LATERAL (
            SELECT COALESCE(
                (SELECT pd.pipeline_def_id FROM pipeline_defs pd
                 WHERE pd.pipeline_code = 'standard_chunk'
                   AND (pd.project_id IS NULL OR pd.project_id = pp.project_id)
                 ORDER BY pd.version DESC LIMIT 1),
                (SELECT pd.pipeline_def_id FROM pipeline_defs pd
                 WHERE pd.pipeline_type = 'asset_import' AND pd.status = 'active'
                   AND (pd.project_id IS NULL OR pd.project_id = pp.project_id)
                 ORDER BY pd.version DESC LIMIT 1)
            ) AS def_id
        ) chosen
        WHERE chosen.def_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM project_pipeline_rules pr
              WHERE pr.project_id = pp.project_id AND pr.pattern = '*'
          )
    """)

    # Migrate existing index_states → document_index_states
    # (one old row → up to 4 new rows, one per backend dimension)
    op.execute("""
        INSERT INTO document_index_states
            (document_id, backend_type, state, indexed_version, target_version,
             last_error, built_at)
        SELECT
            is_old.object_id,
            bt.backend_type,
            CASE bt.backend_type
                WHEN 'fulltext' THEN is_old.fts_state
                WHEN 'vector'   THEN is_old.vector_state
                WHEN 'graph'    THEN is_old.graph_state
                WHEN 'citation' THEN is_old.citation_state
            END,
            is_old.ready_version,
            is_old.stale_version,
            is_old.last_error,
            is_old.last_refreshed_at
        FROM index_states is_old
        CROSS JOIN (VALUES ('fulltext'), ('vector'), ('graph'), ('citation')) AS bt(backend_type)
        WHERE is_old.object_type = 'knowledge_document'
          AND NOT EXISTS (
              SELECT 1 FROM document_index_states dis
              WHERE dis.document_id = is_old.object_id
                AND dis.backend_type = bt.backend_type
          )
    """)


def downgrade() -> None:
    # ── Reverse in dependency order ────────────────────────────────────

    # 3. Data migration is NOT reversed (old tables are preserved)

    # 2.3 processing_jobs: drop new column
    op.drop_column("processing_jobs", "pipeline_def_id")

    # 2.2 knowledge_documents: drop new columns
    op.drop_index("ix_knowledge_docs_folder", table_name="knowledge_documents")
    op.drop_column("knowledge_documents", "content_hash")
    op.drop_constraint(
        "knowledge_documents_source_asset_id_fkey",
        "knowledge_documents", type_="foreignkey",
    )
    op.drop_column("knowledge_documents", "source_asset_id")
    op.drop_constraint(
        "knowledge_documents_pipeline_def_id_fkey",
        "knowledge_documents", type_="foreignkey",
    )
    op.drop_column("knowledge_documents", "pipeline_def_id")
    op.drop_column("knowledge_documents", "folder_path")
    op.drop_column("knowledge_documents", "lang")

    # 2.1 assets: revert CHECK + drop new columns
    op.drop_index("ix_assets_staging_expires", table_name="assets")
    op.drop_index("ix_assets_pool_ref", table_name="assets")
    op.drop_column("assets", "staging_expires_at")
    op.drop_column("assets", "original_pool_ref")
    op.drop_column("assets", "relative_path")
    # Restore original CHECK (allow 'deleted')
    op.drop_constraint("assets_status_check", "assets", type_="check")
    op.create_check_constraint(
        "assets_status_check",
        "assets",
        "status IN ('active', 'archived', 'deleted')",
    )

    # 1. Drop new tables (reverse creation order)
    op.drop_table("document_tag_links")
    op.drop_table("document_tags")
    op.drop_table("document_references")
    op.drop_index("ix_document_index_states_state", table_name="document_index_states")
    op.drop_table("document_index_states")
    op.drop_table("project_pipeline_rules")
    op.drop_index("ix_project_backends_project", table_name="project_backends")
    op.drop_table("project_backends")
    op.drop_table("backend_type_registry")
