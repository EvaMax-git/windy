"""P7 graph_nodes / graph_edges data-access layer.

Provides list/detail queries against the dedicated ``graph_nodes`` and
``graph_edges`` tables created in migration 0003_p7_graph_tables.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session


# ── Graph Nodes ──────────────────────────────────────────────────────────

_LIST_NODES = text(r"""
    SELECT
        gn.node_id,
        gn.project_id,
        gn.node_type,
        gn.node_label,
        gn.node_key,
        gn.source_type,
        gn.source_id,
        gn.content_hash,
        gn.properties_json,
        gn.sensitivity_level,
        gn.status,
        gn.created_at,
        gn.updated_at,
        COUNT(ge.edge_id) AS edge_count
    FROM graph_nodes gn
    LEFT JOIN graph_edges ge
        ON (ge.from_node_id = gn.node_id OR ge.to_node_id = gn.node_id)
        AND ge.relation_status = 'active'
    WHERE gn.status = :status
      AND (:project_id IS NULL OR gn.project_id = :project_id)
      AND (:node_type IS NULL OR gn.node_type = :node_type)
      AND (:search IS NULL OR gn.node_label LIKE '%' || :search || '%')
    GROUP BY gn.node_id, gn.project_id, gn.node_type, gn.node_label,
             gn.node_key, gn.source_type, gn.source_id, gn.content_hash,
             gn.properties_json, gn.sensitivity_level, gn.status,
             gn.created_at, gn.updated_at
    ORDER BY gn.created_at DESC
    LIMIT :limit OFFSET :offset
""")

_COUNT_NODES = text(r"""
    SELECT COUNT(*)
    FROM graph_nodes gn
    WHERE gn.status = :status
      AND (:project_id IS NULL OR gn.project_id = :project_id)
      AND (:node_type IS NULL OR gn.node_type = :node_type)
      AND (:search IS NULL OR gn.node_label LIKE '%' || :search || '%')
""")


def list_graph_nodes(
    db: Session,
    *,
    project_id: UUID | None = None,
    node_type: str | None = None,
    search: str | None = None,
    status: str = "active",
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[dict], int]:
    """Return (items, total) for graph_nodes."""
    offset = (max(page, 1) - 1) * page_size
    total = db.execute(
        _COUNT_NODES,
        {"status": status, "project_id": project_id, "node_type": node_type, "search": search},
    ).scalar_one()

    rows = db.execute(
        _LIST_NODES,
        {
            "status": status,
            "project_id": project_id,
            "node_type": node_type,
            "search": search,
            "limit": page_size,
            "offset": offset,
        },
    ).mappings().all()

    items = [dict(r) for r in rows]
    return items, total or 0


_GET_NODE = text(r"""
    SELECT * FROM graph_nodes WHERE node_id = :node_id
""")


def get_graph_node_table(db: Session, *, node_id: UUID) -> dict | None:
    """Return a single graph_node row or None."""
    row = db.execute(_GET_NODE, {"node_id": node_id}).mappings().first()
    return dict(row) if row else None


# ── Graph Edges ──────────────────────────────────────────────────────────

_LIST_EDGES_ALL = text(r"""
    SELECT
        ge.edge_id,
        ge.project_id,
        ge.from_node_id,
        ge.to_node_id,
        ge.edge_type,
        ge.edge_label,
        ge.weight,
        ge.properties_json,
        ge.relation_status,
        ge.source_type,
        ge.source_id,
        ge.created_at,
        ge.updated_at,
        gn_from.node_label AS from_label,
        gn_to.node_label   AS to_label
    FROM graph_edges ge
    LEFT JOIN graph_nodes gn_from ON gn_from.node_id = ge.from_node_id
    LEFT JOIN graph_nodes gn_to   ON gn_to.node_id   = ge.to_node_id
    WHERE ge.relation_status = :relation_status
      AND (:project_id IS NULL OR ge.project_id = :project_id)
    ORDER BY ge.created_at DESC
    LIMIT :limit OFFSET :offset
""")

_LIST_EDGES_FILTERED = text(r"""
    SELECT
        ge.edge_id,
        ge.project_id,
        ge.from_node_id,
        ge.to_node_id,
        ge.edge_type,
        ge.edge_label,
        ge.weight,
        ge.properties_json,
        ge.relation_status,
        ge.source_type,
        ge.source_id,
        ge.created_at,
        ge.updated_at,
        gn_from.node_label AS from_label,
        gn_to.node_label   AS to_label
    FROM graph_edges ge
    LEFT JOIN graph_nodes gn_from ON gn_from.node_id = ge.from_node_id
    LEFT JOIN graph_nodes gn_to   ON gn_to.node_id   = ge.to_node_id
    WHERE ge.relation_status = :relation_status
      AND (:project_id IS NULL OR ge.project_id = :project_id)
      AND (ge.from_node_id = ANY(:node_ids) OR ge.to_node_id = ANY(:node_ids))
    ORDER BY ge.created_at DESC
    LIMIT :limit OFFSET :offset
""")

_COUNT_EDGES_ALL = text(r"""
    SELECT COUNT(*)
    FROM graph_edges ge
    WHERE ge.relation_status = :relation_status
      AND (:project_id IS NULL OR ge.project_id = :project_id)
""")

_COUNT_EDGES_FILTERED = text(r"""
    SELECT COUNT(*)
    FROM graph_edges ge
    WHERE ge.relation_status = :relation_status
      AND (:project_id IS NULL OR ge.project_id = :project_id)
      AND (ge.from_node_id = ANY(:node_ids) OR ge.to_node_id = ANY(:node_ids))
""")


def list_graph_edges(
    db: Session,
    *,
    project_id: UUID | None = None,
    node_ids: list[str] | None = None,
    relation_status: str = "active",
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[dict], int]:
    """Return (items, total) for graph_edges.

    If *node_ids* is provided, only edges involving those nodes are returned.
    """
    offset = (max(page, 1) - 1) * page_size
    has_filter = bool(node_ids)
    pg_ids = [UUID(i) for i in node_ids] if node_ids else []

    params: dict = {
        "relation_status": relation_status,
        "project_id": project_id,
        "limit": page_size,
        "offset": offset,
    }
    count_params: dict = {
        "relation_status": relation_status,
        "project_id": project_id,
    }

    if has_filter:
        params["node_ids"] = pg_ids
        count_params["node_ids"] = pg_ids
        total = db.execute(_COUNT_EDGES_FILTERED, count_params).scalar_one()
        rows = db.execute(_LIST_EDGES_FILTERED, params).mappings().all()
    else:
        total = db.execute(_COUNT_EDGES_ALL, count_params).scalar_one()
        rows = db.execute(_LIST_EDGES_ALL, params).mappings().all()

    items = [dict(r) for r in rows]
    return items, total or 0
