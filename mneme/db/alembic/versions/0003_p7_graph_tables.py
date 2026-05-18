"""P7 Graph DDL: create graph_nodes + graph_edges tables.

Revision ID: 0003_p7_graph_tables
Revises: 0002_p6_refine_columns
Create Date: 2026-05-05

graph_nodes
-----------
Abstract nodes in the knowledge graph.  Each node is either derived from an
existing domain object (memory, document, chunk, asset, message) or represents
an extracted concept/entity/topic.  Nodes carry a ``node_label`` for human
display, an optional ``node_key`` for project-scoped uniqueness, and a
``properties_json`` payload for schema-flexible attributes.

``embedding vector(1536)`` is nullable — populated in a later phase when
graph-aware embedding is implemented.

graph_edges
-----------
Directed edges between graph_nodes.  Edge types encode semantic relationships:

  * ``semantic_similarity`` — two nodes express similar meaning
  * ``provenance`` — one node was derived from another
  * ``coreference`` — nodes refer to the same real-world entity
  * ``causal`` — one node causes/implies another
  * ``hierarchical`` — parent/child or broader/narrower relation
  * ``temporal`` — time-ordering relation
  * ``custom`` — application-specific edge

``weight numeric(5,4)`` defaults to 1.0 and is constrained to [-1, 1]:

  * +1.0 = strongly supports / confirms
  *  0.0 = neutral / unrelated
  * -1.0 = strongly conflicts / contradicts

``source_type`` / ``source_id`` records provenance of the edge itself (e.g. an
LLM extraction run, a manual review, or a pipeline job).

Design rationale
----------------
* Separate from ``memory_relations`` (which is memory-specific and flat).
  graph_edges is the general-purpose graph layer that will eventually be
  traversed by graph-aware retrieval (Graph RAG, knowledge-graph search, etc.).
* ``index_states.graph_state`` already exists as a placeholder.  graph_nodes
  and graph_edges are the backing storage for that index dimension.
* Project-scoped via ``project_id`` FK — aligns with the existing multi-tenant
  data model.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003_p7_graph_tables"
down_revision: str | Sequence[str] | None = "0002_p6_refine_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ═══════════════════════════════════════════════════════════════════════
# DDL SQL fragments
# ═══════════════════════════════════════════════════════════════════════

_GRAPH_NODES_SQL = r"""
CREATE TABLE graph_nodes (
    node_id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      uuid,
    node_type       varchar(40) NOT NULL,
    node_label      varchar(300) NOT NULL,
    node_key        varchar(160),
    source_type     varchar(40),
    source_id       uuid,
    content_hash    varchar(128),
    properties_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    sensitivity_level varchar(24) NOT NULL DEFAULT 'normal',
    status          varchar(24) NOT NULL DEFAULT 'active',
    embedding       vector(1536),
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),

    CHECK (node_type IN (
        'memory', 'document', 'chunk', 'entity',
        'concept', 'topic', 'asset', 'message'
    )),
    CHECK (sensitivity_level IN (
        'public', 'normal', 'private', 'sensitive', 'secret'
    )),
    CHECK (status IN ('active', 'archived', 'deleted', 'stale')),

    UNIQUE (project_id, node_key)
);
"""

_GRAPH_EDGES_SQL = r"""
CREATE TABLE graph_edges (
    edge_id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      uuid,
    from_node_id    uuid NOT NULL,
    to_node_id      uuid NOT NULL,
    edge_type       varchar(40) NOT NULL,
    edge_label      varchar(300),
    weight          numeric(5,4) NOT NULL DEFAULT 1.0,
    properties_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    relation_status varchar(24) NOT NULL DEFAULT 'active',
    source_type     varchar(40),
    source_id       uuid,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),

    CHECK (edge_type IN (
        'semantic_similarity', 'provenance', 'coreference',
        'causal', 'hierarchical', 'temporal', 'custom'
    )),
    CHECK (weight >= -1.0 AND weight <= 1.0),
    CHECK (relation_status IN ('active', 'resolved', 'cancelled', 'expired')),
    CHECK (from_node_id <> to_node_id),

    UNIQUE (from_node_id, to_node_id, edge_type)
);
"""

_FK_SQL = r"""
ALTER TABLE graph_nodes
    ADD CONSTRAINT fk_graph_nodes_project
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
    ON DELETE SET NULL;

ALTER TABLE graph_edges
    ADD CONSTRAINT fk_graph_edges_project
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
    ON DELETE SET NULL;

ALTER TABLE graph_edges
    ADD CONSTRAINT fk_graph_edges_from_node
    FOREIGN KEY (from_node_id) REFERENCES graph_nodes(node_id)
    ON DELETE CASCADE;

ALTER TABLE graph_edges
    ADD CONSTRAINT fk_graph_edges_to_node
    FOREIGN KEY (to_node_id) REFERENCES graph_nodes(node_id)
    ON DELETE CASCADE;
"""

_INDEX_SQL = r"""
-- Node lookup indexes
CREATE INDEX idx_graph_nodes_project_type
    ON graph_nodes(project_id, node_type, status);

CREATE INDEX idx_graph_nodes_source
    ON graph_nodes(source_type, source_id);

CREATE INDEX idx_graph_nodes_content_hash
    ON graph_nodes(content_hash)
    WHERE content_hash IS NOT NULL;

-- Edge traversal indexes (critical for graph walk performance)
CREATE INDEX idx_graph_edges_from_node
    ON graph_edges(from_node_id, edge_type, relation_status);

CREATE INDEX idx_graph_edges_to_node
    ON graph_edges(to_node_id, edge_type, relation_status);

CREATE INDEX idx_graph_edges_project
    ON graph_edges(project_id, relation_status);
"""

_TRIGGER_SQL = r"""
CREATE TRIGGER trg_graph_nodes_updated_at
    BEFORE UPDATE ON graph_nodes
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_graph_edges_updated_at
    BEFORE UPDATE ON graph_edges
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
"""

# ═══════════════════════════════════════════════════════════════════════
# Downgrade SQL
# ═══════════════════════════════════════════════════════════════════════

_DOWNGRADE_SQL = r"""
DROP TRIGGER IF EXISTS trg_graph_edges_updated_at ON graph_edges;
DROP TRIGGER IF EXISTS trg_graph_nodes_updated_at ON graph_nodes;
DROP TABLE IF EXISTS graph_edges CASCADE;
DROP TABLE IF EXISTS graph_nodes CASCADE;
"""


def upgrade() -> None:
    op.execute(_GRAPH_NODES_SQL)
    op.execute(_GRAPH_EDGES_SQL)
    op.execute(_FK_SQL)
    op.execute(_INDEX_SQL)
    op.execute(_TRIGGER_SQL)


def downgrade() -> None:
    op.execute(_DOWNGRADE_SQL)
