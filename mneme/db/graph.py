"""P7 Graph API data-access layer.

Provides graph-traversal queries against ``memories`` (nodes) and
``memory_relations`` (edges).  All queries use raw SQL for performance
and compatibility with the project's existing patterns.

Graph model
-----------
* nodes → ``memories`` table (joined with projects for project_code)
* edges → ``memory_relations`` table (joined with memories for titles)
"""

from __future__ import annotations

import json
import math
from collections import defaultdict, deque
from typing import Any
from uuid import UUID

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session
from mneme.db.compat import PortableUUID as PG_UUID


def _coerce_uuid(value):
    if value is None or isinstance(value, UUID):
        return value
    return UUID(str(value))


# ═══════════════════════════════════════════════════════════════════════════
# Row mapping
# ═══════════════════════════════════════════════════════════════════════════

def _row_to_dict(row) -> dict:
    """Map a SQLAlchemy row to a plain dict."""
    if row is None:
        return {}
    return dict(row._mapping)


# ═══════════════════════════════════════════════════════════════════════════
# Node queries
# ═══════════════════════════════════════════════════════════════════════════

_NODE_COLUMNS = """
    m.memory_id,
    m.project_id,
    m.canonical_key,
    m.title,
    m.memory_text,
    m.current_version,
    m.sensitivity_level,
    m.status,
    m.node_type,
    m.quality_score,
    m.search_weight,
    m.activated_at,
    m.expired_at,
    m.created_at,
    m.updated_at
"""

_GET_NODE = text(f"""
    SELECT {_NODE_COLUMNS}
    FROM memories m
    WHERE m.memory_id = :mid
""").bindparams(bindparam("mid", type_=PG_UUID(as_uuid=True)))

_NODE_EDGE_COUNTS = text("""
    SELECT
        COALESCE(SUM(CASE WHEN from_memory_id = :mid THEN 1 ELSE 0 END), 0) AS out_degree,
        COALESCE(SUM(CASE WHEN to_memory_id = :mid THEN 1 ELSE 0 END), 0) AS in_degree,
        COUNT(*) AS total_relations
    FROM memory_relations
    WHERE relation_status = 'active'
      AND (from_memory_id = :mid OR to_memory_id = :mid)
""").bindparams(bindparam("mid", type_=PG_UUID(as_uuid=True)))

_LIST_NODES_COUNT = text("""
    SELECT count(*)
    FROM memories m
    WHERE (:project_id IS NULL OR m.project_id = :project_id)
      AND (:status IS NULL OR m.status = :status)
      AND (:search IS NULL OR m.title LIKE '%' || :search || '%'
           OR m.memory_text LIKE '%' || :search || '%')
""").bindparams(bindparam("project_id", type_=PG_UUID(as_uuid=True)))

_LIST_NODES = text(f"""
    SELECT {_NODE_COLUMNS}
    FROM memories m
    WHERE (:project_id IS NULL OR m.project_id = :project_id)
      AND (:status IS NULL OR m.status = :status)
      AND (:search IS NULL OR m.title LIKE '%' || :search || '%'
           OR m.memory_text LIKE '%' || :search || '%')
    ORDER BY m.created_at DESC
    LIMIT :page_size OFFSET :offset
""").bindparams(bindparam("project_id", type_=PG_UUID(as_uuid=True)))

_LIST_ACTIVE_RELATIONS_FOR_NODES = text("""
    SELECT
        mr.memory_relation_id,
        mr.from_memory_id,
        mr.to_memory_id,
        mr.relation_type,
        mr.relation_status,
        mr.reason,
        mr.metadata_json,
        mr.created_at,
        fm.title AS from_title,
        tm.title AS to_title,
        fm.canonical_key AS from_canonical_key,
        tm.canonical_key AS to_canonical_key
    FROM memory_relations mr
    LEFT JOIN memories fm ON fm.memory_id = mr.from_memory_id
    LEFT JOIN memories tm ON tm.memory_id = mr.to_memory_id
    WHERE mr.relation_status = 'active'
      AND (mr.from_memory_id = ANY(:mids) OR mr.to_memory_id = ANY(:mids))
""")


# ═══════════════════════════════════════════════════════════════════════════
# Edge queries
# ═══════════════════════════════════════════════════════════════════════════

_EDGE_COLUMNS = """
    mr.memory_relation_id,
    mr.from_memory_id,
    mr.to_memory_id,
    mr.relation_type,
    mr.relation_status,
    mr.reason,
    mr.metadata_json,
    mr.created_at,
    fm.title AS from_title,
    tm.title AS to_title,
    fm.canonical_key AS from_canonical_key,
    tm.canonical_key AS to_canonical_key
"""

_GET_EDGE = text(f"""
    SELECT {_EDGE_COLUMNS}
    FROM memory_relations mr
    LEFT JOIN memories fm ON fm.memory_id = mr.from_memory_id
    LEFT JOIN memories tm ON tm.memory_id = mr.to_memory_id
    WHERE mr.memory_relation_id = :rid
""").bindparams(bindparam("rid", type_=PG_UUID(as_uuid=True)))

_LIST_EDGES_COUNT = text("""
    SELECT count(*)
    FROM memory_relations mr
    WHERE (:memory_id IS NULL OR mr.from_memory_id = :memory_id OR mr.to_memory_id = :memory_id)
      AND (:relation_type IS NULL OR mr.relation_type = :relation_type)
      AND (:relation_status IS NULL OR mr.relation_status = :relation_status)
      AND (:project_id IS NULL
           OR mr.from_memory_id IN (SELECT memory_id FROM memories WHERE project_id = :project_id))
""").bindparams(
    bindparam("memory_id", type_=PG_UUID(as_uuid=True)),
    bindparam("project_id", type_=PG_UUID(as_uuid=True)),
)

_LIST_EDGES = text(f"""
    SELECT {_EDGE_COLUMNS}
    FROM memory_relations mr
    LEFT JOIN memories fm ON fm.memory_id = mr.from_memory_id
    LEFT JOIN memories tm ON tm.memory_id = mr.to_memory_id
    WHERE (:memory_id IS NULL OR mr.from_memory_id = :memory_id OR mr.to_memory_id = :memory_id)
      AND (:relation_type IS NULL OR mr.relation_type = :relation_type)
      AND (:relation_status IS NULL OR mr.relation_status = :relation_status)
      AND (:project_id IS NULL
           OR mr.from_memory_id IN (SELECT memory_id FROM memories WHERE project_id = :project_id))
    ORDER BY mr.created_at DESC
    LIMIT :page_size OFFSET :offset
""").bindparams(
    bindparam("memory_id", type_=PG_UUID(as_uuid=True)),
    bindparam("project_id", type_=PG_UUID(as_uuid=True)),
)

# ═══════════════════════════════════════════════════════════════════════════
# Graph traversal — adjacency query
# ═══════════════════════════════════════════════════════════════════════════

_ALL_ACTIVE_EDGES = text(f"""
    SELECT {_EDGE_COLUMNS}
    FROM memory_relations mr
    LEFT JOIN memories fm ON fm.memory_id = mr.from_memory_id
    LEFT JOIN memories tm ON tm.memory_id = mr.to_memory_id
    WHERE mr.relation_status = 'active'
""")


# ═══════════════════════════════════════════════════════════════════════════
# Graph summary
# ═══════════════════════════════════════════════════════════════════════════

_GRAPH_SUMMARY_NODES = text("""
    SELECT
        COUNT(*) AS total_nodes,
        COALESCE(SUM(CASE WHEN m.status = 'active' THEN 1 ELSE 0 END), 0) AS active_nodes
    FROM memories m
    WHERE (:project_id IS NULL OR m.project_id = :project_id)
""").bindparams(bindparam("project_id", type_=PG_UUID(as_uuid=True)))

_GRAPH_SUMMARY_EDGES = text("""
    SELECT
        COUNT(*) AS total_edges,
        COALESCE(SUM(CASE WHEN mr.relation_status = 'active' THEN 1 ELSE 0 END), 0) AS active_edges
    FROM memory_relations mr
    WHERE (:project_id IS NULL
           OR mr.from_memory_id IN (SELECT memory_id FROM memories WHERE project_id = :project_id))
""").bindparams(bindparam("project_id", type_=PG_UUID(as_uuid=True)))

_GRAPH_SUMMARY_RELATION_TYPES = text("""
    SELECT mr.relation_type, COUNT(*) AS cnt
    FROM memory_relations mr
    WHERE mr.relation_status = 'active'
      AND (:project_id IS NULL
           OR mr.from_memory_id IN (SELECT memory_id FROM memories WHERE project_id = :project_id))
    GROUP BY mr.relation_type
""").bindparams(bindparam("project_id", type_=PG_UUID(as_uuid=True)))

_GRAPH_DEGREE_STATS = text("""
    SELECT
        COALESCE(MAX(deg.cnt), 0) AS max_degree,
        COALESCE(AVG(deg.cnt * 1.0), 0.0) AS avg_degree
    FROM (
        SELECT COUNT(*) AS cnt
        FROM memory_relations
        WHERE relation_status = 'active'
        GROUP BY from_memory_id
        UNION ALL
        SELECT COUNT(*) AS cnt
        FROM memory_relations
        WHERE relation_status = 'active'
        GROUP BY to_memory_id
    ) deg
""")

_GRAPH_ISOLATED_COUNT = text("""
    SELECT COUNT(*)
    FROM memories m
    WHERE (:project_id IS NULL OR m.project_id = :project_id)
      AND NOT EXISTS (
        SELECT 1 FROM memory_relations mr
        WHERE mr.relation_status = 'active'
          AND (mr.from_memory_id = m.memory_id OR mr.to_memory_id = m.memory_id)
      )
""").bindparams(bindparam("project_id", type_=PG_UUID(as_uuid=True)))


# ═══════════════════════════════════════════════════════════════════════════
# Public API — Node operations
# ═══════════════════════════════════════════════════════════════════════════

def get_graph_node(
    db: Session, *, memory_id: UUID
) -> dict | None:
    """Get a single graph node (memory) with edge counts."""
    row = db.execute(_GET_NODE, {"mid": _coerce_uuid(memory_id)}).first()
    if row is None:
        return None

    node = _row_to_dict(row)

    # Get edge degree counts
    counts_row = db.execute(
        _NODE_EDGE_COUNTS, {"mid": _coerce_uuid(memory_id)}
    ).first()
    if counts_row:
        node["out_degree"] = counts_row[0] or 0
        node["in_degree"] = counts_row[1] or 0
        node["total_relations"] = counts_row[2] or 0
    else:
        node["out_degree"] = 0
        node["in_degree"] = 0
        node["total_relations"] = 0

    return node


def list_graph_nodes(
    db: Session,
    *,
    project_id: UUID | None = None,
    status: str | None = None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[dict], int]:
    """List graph nodes (memories) with pagination and filtering."""
    params = {
        "project_id": _coerce_uuid(project_id),
        "status": status,
        "search": search,
    }
    total = db.execute(_LIST_NODES_COUNT, params).scalar_one()
    offset = (page - 1) * page_size
    rows = db.execute(
        _LIST_NODES, {**params, "page_size": page_size, "offset": offset}
    ).all()

    nodes = [_row_to_dict(r) for r in rows]

    # Batch-fetch edge counts for all nodes
    if nodes:
        mids = [_coerce_uuid(n["memory_id"]) for n in nodes]
        edge_counts = _batch_edge_counts(db, mids)
        for node in nodes:
            ec = edge_counts.get(str(node["memory_id"]), {})
            node["out_degree"] = ec.get("out_degree", 0)
            node["in_degree"] = ec.get("in_degree", 0)
            node["total_relations"] = ec.get("total_relations", 0)

    return nodes, total


def _batch_edge_counts(db: Session, memory_ids: list[UUID]) -> dict[str, dict]:
    """Batch-fetch edge counts for multiple memory IDs."""
    if not memory_ids:
        return {}

    # Build a dynamic query since ANY(:mids) doesn't work well with bindparam
    placeholders = ", ".join([f"'{str(mid)}'" for mid in memory_ids])
    query = text(f"""
        SELECT
            mid,
            SUM(out_c) AS out_degree,
            SUM(in_c) AS in_degree,
            SUM(out_c) + SUM(in_c) AS total_relations
        FROM (
            SELECT from_memory_id AS mid, 1 AS out_c, 0 AS in_c
            FROM memory_relations
            WHERE relation_status = 'active' AND from_memory_id IN ({placeholders})
            UNION ALL
            SELECT to_memory_id AS mid, 0 AS out_c, 1 AS in_c
            FROM memory_relations
            WHERE relation_status = 'active' AND to_memory_id IN ({placeholders})
        ) t
        GROUP BY mid
    """)

    rows = db.execute(query).all()
    result: dict[str, dict] = {}
    for row in rows:
        result[str(row[0])] = {
            "out_degree": row[1] or 0,
            "in_degree": row[2] or 0,
            "total_relations": row[3] or 0,
        }
    return result


# ═══════════════════════════════════════════════════════════════════════════
# Public API — Edge operations
# ═══════════════════════════════════════════════════════════════════════════

def get_graph_edge(
    db: Session, *, relation_id: UUID
) -> dict | None:
    """Get a single graph edge (memory_relation) with denormalized titles."""
    row = db.execute(_GET_EDGE, {"rid": _coerce_uuid(relation_id)}).first()
    if row is None:
        return None
    edge = _row_to_dict(row)
    _normalize_metadata(edge)
    return edge


def list_graph_edges(
    db: Session,
    *,
    memory_id: UUID | None = None,
    relation_type: str | None = None,
    relation_status: str | None = None,
    project_id: UUID | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[dict], int]:
    """List graph edges (memory_relations) with pagination and filtering."""
    params = {
        "memory_id": _coerce_uuid(memory_id),
        "relation_type": relation_type,
        "relation_status": relation_status,
        "project_id": _coerce_uuid(project_id),
    }
    total = db.execute(_LIST_EDGES_COUNT, params).scalar_one()
    offset = (page - 1) * page_size
    rows = db.execute(
        _LIST_EDGES, {**params, "page_size": page_size, "offset": offset}
    ).all()
    edges = [_row_to_dict(r) for r in rows]
    for edge in edges:
        _normalize_metadata(edge)
    return edges, total


def _normalize_metadata(edge: dict) -> None:
    """Normalize metadata_json field from string to dict."""
    if "metadata_json" in edge and isinstance(edge["metadata_json"], str):
        try:
            edge["metadata_json"] = json.loads(edge["metadata_json"])
        except (json.JSONDecodeError, TypeError):
            edge["metadata_json"] = {}


# ═══════════════════════════════════════════════════════════════════════════
# Public API — Graph traversal
# ═══════════════════════════════════════════════════════════════════════════

def _load_full_graph(
    db: Session,
    *,
    project_id: UUID | None = None,
    relation_types: list[str] | None = None,
) -> tuple[dict[UUID, dict], dict[UUID, list[dict]], dict[UUID, list[dict]]]:
    """Load the full active graph into memory for traversal.

    Returns:
        nodes: dict[memory_id, node_data]
        outgoing_adj: dict[memory_id, list of edge dicts]
        incoming_adj: dict[memory_id, list of edge dicts]
    """
    # Load all active edges
    rows = db.execute(_ALL_ACTIVE_EDGES).all()
    edges = [_row_to_dict(r) for r in rows]
    for e in edges:
        _normalize_metadata(e)

    # Filter by relation_types if specified
    if relation_types:
        edges = [e for e in edges if e["relation_type"] in relation_types]

    # Filter by project_id if specified
    if project_id:
        pid = str(project_id)
        # We need node project_ids — collect all involved memory_ids first
        involved_mids = set()
        for e in edges:
            involved_mids.add(str(e["from_memory_id"]))
            involved_mids.add(str(e["to_memory_id"]))

        # Batch fetch project_ids for involved nodes
        if involved_mids:
            placeholders = ", ".join([f"'{mid}'" for mid in involved_mids])
            query = text(f"""
                SELECT memory_id, project_id
                FROM memories
                WHERE memory_id IN ({placeholders})
            """)
            node_rows = db.execute(query).all()
            node_projects = {str(r[0]): str(r[1]) if r[1] else None for r in node_rows}
        else:
            node_projects = {}

        # Keep only edges where at least one node belongs to the project
        edges = [
            e for e in edges
            if node_projects.get(str(e["from_memory_id"])) == pid
            or node_projects.get(str(e["to_memory_id"])) == pid
        ]

    # Build adjacency lists
    outgoing_adj: dict[UUID, list[dict]] = defaultdict(list)
    incoming_adj: dict[UUID, list[dict]] = defaultdict(list)
    node_ids: set[UUID] = set()

    for e in edges:
        from_id = _coerce_uuid(e["from_memory_id"])
        to_id = _coerce_uuid(e["to_memory_id"])
        outgoing_adj[from_id].append(e)
        incoming_adj[to_id].append(e)
        node_ids.add(from_id)
        node_ids.add(to_id)

    # Load node data for all involved nodes
    nodes: dict[UUID, dict] = {}
    if node_ids:
        placeholders = ", ".join([f"'{str(mid)}'" for mid in node_ids])
        query = text(f"""
            SELECT {_NODE_COLUMNS}
            FROM memories m
            WHERE m.memory_id IN ({placeholders})
        """)
        node_rows = db.execute(query).all()
        for row in node_rows:
            node = _row_to_dict(row)
            nodes[_coerce_uuid(node["memory_id"])] = node

    return nodes, outgoing_adj, incoming_adj


def query_neighborhood(
    db: Session,
    *,
    source_memory_id: UUID,
    max_depth: int = 3,
    direction: str = "both",
    relation_types: list[str] | None = None,
    project_id: UUID | None = None,
) -> dict:
    """BFS-based neighborhood query around a source node."""
    nodes, outgoing_adj, incoming_adj = _load_full_graph(
        db, project_id=project_id, relation_types=relation_types,
    )

    source = _coerce_uuid(source_memory_id)
    visited: dict[UUID, int] = {}  # node_id → depth
    discovered_edges: list[dict] = []
    neighbor_nodes: list[dict] = []

    queue: deque[tuple[UUID, int, str | None, UUID | None]] = deque()
    queue.append((source, 0, None, None))  # (node, depth, via_rel_type, via_rel_id)
    visited[source] = 0

    while queue:
        current, depth, via_type, via_rid = queue.popleft()
        if depth > 0:
            node = nodes.get(current, {})
            ni = {
                "memory_id": str(current),
                "title": node.get("title"),
                "canonical_key": node.get("canonical_key"),
                "status": node.get("status", "unknown"),
                "depth": depth,
                "via_relation_type": via_type,
                "via_relation_id": str(via_rid) if via_rid else None,
            }
            neighbor_nodes.append(ni)

        if depth >= max_depth:
            continue

        # Traverse outgoing edges
        if direction in ("outgoing", "both"):
            for edge in outgoing_adj.get(current, []):
                to_id = _coerce_uuid(edge["to_memory_id"])
                if to_id not in visited:
                    visited[to_id] = depth + 1
                    queue.append((to_id, depth + 1, edge["relation_type"],
                                   _coerce_uuid(edge["memory_relation_id"])))
                    discovered_edges.append(edge)

        # Traverse incoming edges
        if direction in ("incoming", "both"):
            for edge in incoming_adj.get(current, []):
                from_id = _coerce_uuid(edge["from_memory_id"])
                if from_id not in visited:
                    visited[from_id] = depth + 1
                    queue.append((from_id, depth + 1, edge["relation_type"],
                                   _coerce_uuid(edge["memory_relation_id"])))
                    discovered_edges.append(edge)

    return {
        "source_memory_id": str(source),
        "max_depth": max_depth,
        "nodes": neighbor_nodes,
        "edges": discovered_edges,
        "total_nodes": len(neighbor_nodes),
        "total_edges": len(discovered_edges),
    }


def query_shortest_paths(
    db: Session,
    *,
    source_memory_id: UUID,
    target_memory_id: UUID,
    max_depth: int = 5,
    relation_types: list[str] | None = None,
    project_id: UUID | None = None,
) -> dict:
    """BFS-based shortest path query between two nodes.

    Returns up to 10 shortest paths.
    """
    nodes, outgoing_adj, incoming_adj = _load_full_graph(
        db, project_id=project_id, relation_types=relation_types,
    )

    source = _coerce_uuid(source_memory_id)
    target = _coerce_uuid(target_memory_id)

    if source == target:
        return {
            "source_memory_id": str(source),
            "target_memory_id": str(target),
            "max_depth": max_depth,
            "paths": [],
            "total_paths": 0,
            "shortest_path_length": 0,
        }

    # Build adjacency as undirected for BFS
    adj: dict[UUID, list[tuple[UUID, dict, str]]] = defaultdict(list)
    # (neighbor, edge, direction) where direction="out" or "in"
    for from_id, edges in outgoing_adj.items():
        for e in edges:
            to_id = _coerce_uuid(e["to_memory_id"])
            adj[from_id].append((to_id, e, "out"))
    for to_id, edges in incoming_adj.items():
        for e in edges:
            from_id = _coerce_uuid(e["from_memory_id"])
            adj[to_id].append((from_id, e, "in"))

    # BFS to find shortest path length
    visited_dist: dict[UUID, int] = {}
    queue: deque[tuple[UUID, int]] = deque()
    queue.append((source, 0))
    visited_dist[source] = 0

    while queue:
        current, dist = queue.popleft()
        if dist >= max_depth:
            continue
        for neighbor, _edge, _dir in adj.get(current, []):
            if neighbor not in visited_dist:
                visited_dist[neighbor] = dist + 1
                queue.append((neighbor, dist + 1))

    if target not in visited_dist:
        return {
            "source_memory_id": str(source),
            "target_memory_id": str(target),
            "max_depth": max_depth,
            "paths": [],
            "total_paths": 0,
            "shortest_path_length": None,
        }

    shortest_dist = visited_dist[target]

    # DFS to collect all shortest paths (up to 10)
    paths: list[dict] = []
    path_nodes: list[UUID] = [source]
    path_edges: list[dict] = []

    def dfs(current: UUID, depth: int) -> None:
        if len(paths) >= 10:
            return
        if depth > shortest_dist:
            return
        if current == target and depth == shortest_dist:
            # Build path response
            pn = []
            pe = []
            for i, nid in enumerate(path_nodes):
                node = nodes.get(nid, {})
                pn.append({
                    "memory_id": str(nid),
                    "title": node.get("title"),
                    "canonical_key": node.get("canonical_key"),
                    "step": i,
                })
            for i, edge in enumerate(path_edges):
                pe.append({
                    "relation_id": str(_coerce_uuid(edge["memory_relation_id"])),
                    "from_memory_id": str(_coerce_uuid(edge["from_memory_id"])),
                    "to_memory_id": str(_coerce_uuid(edge["to_memory_id"])),
                    "relation_type": edge["relation_type"],
                    "step": i,
                })
            paths.append({
                "source_memory_id": str(source),
                "target_memory_id": str(target),
                "path_length": shortest_dist,
                "nodes": pn,
                "edges": pe,
            })
            return

        for neighbor, edge, _dir in adj.get(current, []):
            if neighbor not in path_nodes:
                path_nodes.append(neighbor)
                path_edges.append(edge)
                dfs(neighbor, depth + 1)
                path_edges.pop()
                path_nodes.pop()

    if source in adj:
        dfs(source, 0)

    return {
        "source_memory_id": str(source),
        "target_memory_id": str(target),
        "max_depth": max_depth,
        "paths": paths,
        "total_paths": len(paths),
        "shortest_path_length": shortest_dist,
    }


def query_connected(
    db: Session,
    *,
    source_memory_id: UUID,
    target_memory_id: UUID,
    max_depth: int = 10,
    relation_types: list[str] | None = None,
    project_id: UUID | None = None,
) -> dict:
    """Check if two nodes are connected and return the distance."""
    nodes, outgoing_adj, incoming_adj = _load_full_graph(
        db, project_id=project_id, relation_types=relation_types,
    )

    source = _coerce_uuid(source_memory_id)
    target = _coerce_uuid(target_memory_id)

    if source == target:
        return {
            "source_memory_id": str(source),
            "target_memory_id": str(target),
            "connected": True,
            "distance": 0,
        }

    # Build undirected adjacency
    adj: dict[UUID, list[UUID]] = defaultdict(list)
    for from_id, edges in outgoing_adj.items():
        for e in edges:
            to_id = _coerce_uuid(e["to_memory_id"])
            adj[from_id].append(to_id)
            adj[to_id].append(from_id)

    # BFS
    visited: dict[UUID, int] = {}
    queue: deque[tuple[UUID, int]] = deque()
    queue.append((source, 0))
    visited[source] = 0

    while queue:
        current, dist = queue.popleft()
        if current == target:
            return {
                "source_memory_id": str(source),
                "target_memory_id": str(target),
                "connected": True,
                "distance": dist,
            }
        if dist >= max_depth:
            continue
        for neighbor in adj.get(current, []):
            if neighbor not in visited:
                visited[neighbor] = dist + 1
                queue.append((neighbor, dist + 1))

    return {
        "source_memory_id": str(source),
        "target_memory_id": str(target),
        "connected": False,
        "distance": None,
    }


def query_subgraph(
    db: Session,
    *,
    node_ids: list[UUID],
    relation_types: list[str] | None = None,
) -> dict:
    """Get the subgraph induced by a set of nodes — all nodes and edges between them."""
    mids = [_coerce_uuid(nid) for nid in node_ids]

    # Fetch nodes
    placeholders = ", ".join([f"'{str(mid)}'" for mid in mids])
    query = text(f"""
        SELECT {_NODE_COLUMNS}
        FROM memories m
        WHERE m.memory_id IN ({placeholders})
        ORDER BY m.created_at DESC
    """)
    rows = db.execute(query).all()
    node_list = [_row_to_dict(r) for r in rows]

    # Fetch edges between the specified nodes
    edge_query = text(f"""
        SELECT {_EDGE_COLUMNS}
        FROM memory_relations mr
        LEFT JOIN memories fm ON fm.memory_id = mr.from_memory_id
        LEFT JOIN memories tm ON tm.memory_id = mr.to_memory_id
        WHERE mr.relation_status = 'active'
          AND mr.from_memory_id IN ({placeholders})
          AND mr.to_memory_id IN ({placeholders})
        ORDER BY mr.created_at DESC
    """)
    edge_rows = db.execute(edge_query).all()
    edge_list = [_row_to_dict(r) for r in edge_rows]
    for e in edge_list:
        _normalize_metadata(e)

    # Filter by relation_types if specified
    if relation_types:
        edge_list = [e for e in edge_list if e["relation_type"] in relation_types]

    return {
        "node_ids": [str(nid) for nid in node_ids],
        "nodes": node_list,
        "edges": edge_list,
        "total_nodes": len(node_list),
        "total_edges": len(edge_list),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Public API — Graph summary
# ═══════════════════════════════════════════════════════════════════════════

def get_graph_summary(
    db: Session, *, project_id: UUID | None = None
) -> dict:
    """Get aggregated graph statistics."""
    pid = _coerce_uuid(project_id)
    params = {"project_id": pid}

    # Node counts
    node_row = db.execute(_GRAPH_SUMMARY_NODES, params).first()
    total_nodes = node_row[0] if node_row else 0
    active_nodes = node_row[1] if node_row else 0

    # Edge counts
    edge_row = db.execute(_GRAPH_SUMMARY_EDGES, params).first()
    total_edges = edge_row[0] if edge_row else 0
    active_edges = edge_row[1] if edge_row else 0

    # Relation type breakdown
    type_rows = db.execute(_GRAPH_SUMMARY_RELATION_TYPES, params).all()
    relation_type_counts: dict[str, int] = {}
    for row in type_rows:
        relation_type_counts[row[0]] = row[1]

    # Degree stats
    degree_row = db.execute(_GRAPH_DEGREE_STATS).first()
    max_degree = degree_row[0] if degree_row else 0
    avg_degree = float(degree_row[1]) if degree_row and degree_row[1] else 0.0

    # Isolated nodes
    isolated = db.execute(_GRAPH_ISOLATED_COUNT, params).scalar_one()

    return {
        "total_nodes": total_nodes,
        "total_edges": total_edges,
        "active_nodes": active_nodes,
        "active_edges": active_edges,
        "relation_type_counts": relation_type_counts,
        "isolated_nodes": isolated,
        "max_degree": max_degree,
        "avg_degree": round(avg_degree, 2),
        "project_id": str(pid) if pid else None,
    }
