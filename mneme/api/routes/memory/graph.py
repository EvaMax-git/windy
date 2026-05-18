"""P7 Graph API — graph query + node/edge management endpoints.

Powered by GraphEngine (NetworkX: PPR, community detection, shortest paths).

Endpoints
---------
* ``GET    /api/v4/graph``                           — consolidated graph data for visualization
* ``GET    /api/v4/graph/nodes``                      — list graph nodes (paginated)
* ``GET    /api/v4/graph/nodes/{memory_id}``          — node detail + edge counts
* ``GET    /api/v4/graph/nodes/{memory_id}/neighbors`` — N-hop neighborhood (via GraphEngine)
* ``GET    /api/v4/graph/edges``                      — list edges (paginated)
* ``GET    /api/v4/graph/edges/{relation_id}``        — edge detail
* ``POST   /api/v4/graph/query``                      — graph traversal (neighborhood / paths / connected / subgraph / ppr / community)
* ``GET    /api/v4/graph/summary``                    — graph statistics
* ``POST   /api/v4/graph/nodes``                      — create a node
* ``PATCH  /api/v4/graph/nodes/{memory_id}``          — update node attributes
* ``POST   /api/v4/graph/edges``                      — create an edge
* ``DELETE /api/v4/graph/edges/{relation_id}``        — remove an edge
* ``POST   /api/v4/graph/ppr``                        — Personalized PageRank traversal
* ``POST   /api/v4/graph/community``                  — Louvain community detection
* ``POST   /api/v4/graph/analyze``                    — full suite graph analysis
"""

from __future__ import annotations

import math
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mneme.api.context import RequestContext, get_request_context
from mneme.api.errors import ApiError
from mneme.api.schemas import envelope
from mneme.db.base import get_db
from mneme.db.graph import (
    get_graph_edge,
    get_graph_node,
    get_graph_summary,
    list_graph_edges,
    list_graph_nodes,
    query_subgraph as _sql_subgraph,
)
from mneme.db.graph_tables import (
    list_graph_nodes as list_gn,
    list_graph_edges as list_ge,
)
from mneme.db.memory_relations import (
    cancel_relation,
    create_memory_relation,
    get_memory_relation,
)
from mneme.db.memories import (
    create_memory,
    get_memory,
    update_memory,
)
from mneme.graph_engine import GraphEngine
from mneme.schemas.common import PageInfo, PaginationParams
from mneme.schemas.graph import (
    GraphEdgeCreate,
    GraphEdgeListResponse,
    GraphEdgeRead,
    GraphNodeCreate,
    GraphNodeListResponse,
    GraphNodeRead,
    GraphNodeUpdate,
    GraphQueryMode,
    GraphQueryRequest,
    GraphSubgraphResponse,
    GraphSummary,
)
from mneme.schemas.memories import MemoryCreate, MemoryUpdate
from mneme.schemas.memory_relations import MemoryRelationCreate

router = APIRouter(prefix="/graph", tags=["graph"])


def _get_graph_engine(db: Session = Depends(get_db)) -> GraphEngine:
    """Dependency injection for GraphEngine."""
    return GraphEngine(db)


def _page_info(total: int, page: int, page_size: int) -> PageInfo:
    """Build a PageInfo model for paginated responses."""
    total_pages = max(1, math.ceil(total / max(page_size, 1)))
    return PageInfo(
        page=page,
        page_size=page_size,
        total_items=total,
        total_pages=total_pages,
        has_next=page < total_pages,
        has_previous=page > 1,
    )


def _parse_relation_types(relation_types: str | None) -> list[str] | None:
    """Parse a comma-separated relation_types query param into a list."""
    if not relation_types:
        return None
    return [rt.strip() for rt in relation_types.split(",") if rt.strip()]


# ═══════════════════════════════════════════════════════════════════════════
# GET /graph — consolidated graph data for visualization
# ═══════════════════════════════════════════════════════════════════════════

@router.get("", response_model=dict)
def get_graph_data_endpoint(
    node_type: str | None = None,
    search: str | None = None,
    project_id: UUID | None = None,
    limit: int = 200,
    depth: int = 2,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Return consolidated graph data (nodes + edges) for front-end visualization."""
    gn_items, gn_total = list_gn(
        db,
        project_id=project_id,
        node_type=node_type,
        search=search,
        status="active",
        page=1,
        page_size=min(limit, 500),
    )

    node_ids = [str(item["node_id"]) for item in gn_items]

    ge_items, ge_total = list_ge(
        db,
        project_id=project_id,
        node_ids=node_ids if node_ids else None,
        relation_status="active",
        page=1,
        page_size=min(limit * 3, 2000),
    )

    nodes = []
    for item in gn_items:
        nodes.append({
            "node_id": str(item["node_id"]),
            "node_type": item.get("node_type", "memory"),
            "label": item.get("node_label", ""),
            "description": (
                item.get("properties_json", {}).get("description")
                if isinstance(item.get("properties_json"), dict)
                else None
            ),
            "project_id": str(item["project_id"]) if item.get("project_id") else None,
            "source_id": str(item["source_id"]) if item.get("source_id") else None,
            "properties": item.get("properties_json", {}),
            "created_at": item["created_at"].isoformat() if item.get("created_at") else None,
            "updated_at": item["updated_at"].isoformat() if item.get("updated_at") else None,
        })

    edges = []
    for item in ge_items:
        edges.append({
            "edge_id": str(item["edge_id"]),
            "from_node_id": str(item["from_node_id"]),
            "to_node_id": str(item["to_node_id"]),
            "relation_type": item.get("edge_type", "custom"),
            "weight": float(item.get("weight", 1.0)),
            "label": item.get("edge_label"),
            "properties": item.get("properties_json", {}),
            "created_at": item["created_at"].isoformat() if item.get("created_at") else None,
        })

    data = {
        "nodes": nodes,
        "edges": edges,
        "total_nodes": gn_total,
        "total_edges": ge_total,
    }

    return envelope(data, request_id=context.request_id, correlation_id=context.correlation_id)


# ═══════════════════════════════════════════════════════════════════════════
# GET /graph/nodes — list graph nodes
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/nodes", response_model=dict)
def list_nodes_endpoint(
    project_id: UUID | None = None,
    status: str | None = None,
    search: str | None = None,
    pagination: PaginationParams = Depends(),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """List graph nodes (memories) with edge-degree counts."""
    items, total = list_graph_nodes(
        db,
        project_id=project_id,
        status=status,
        search=search,
        page=pagination.page,
        page_size=pagination.page_size,
    )

    nodes = [GraphNodeRead.model_validate(item) for item in items]
    pi = _page_info(total, pagination.page, pagination.page_size)
    data = GraphNodeListResponse(items=nodes, page_info=pi)

    return envelope(data.model_dump(mode="json"), request_id=context.request_id, correlation_id=context.correlation_id)


# ═══════════════════════════════════════════════════════════════════════════
# GET /graph/nodes/{memory_id} — node detail
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/nodes/{memory_id}", response_model=dict)
def get_node_endpoint(
    memory_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Get a single graph node by memory_id, including edge-degree counts."""
    node = get_graph_node(db, memory_id=memory_id)
    if node is None:
        raise ApiError(404, "bad_request", f"graph node (memory) {memory_id} not found")

    data = GraphNodeRead.model_validate(node)
    return envelope(data.model_dump(mode="json"), request_id=context.request_id, correlation_id=context.correlation_id)


# ═══════════════════════════════════════════════════════════════════════════
# GET /graph/nodes/{memory_id}/neighbors — N-hop neighborhood (via GraphEngine)
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/nodes/{memory_id}/neighbors", response_model=dict)
def get_node_neighbors_endpoint(
    memory_id: UUID,
    max_depth: int = 2,
    direction: str = "both",
    relation_types: str | None = None,
    project_id: UUID | None = None,
    engine: GraphEngine = Depends(_get_graph_engine),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Get the N-hop neighborhood around a graph node via GraphEngine.

    Query parameters:
    * ``max_depth`` — traversal depth (1-5, default 2)
    * ``direction`` — ``outgoing``, ``incoming``, or ``both`` (default)
    * ``relation_types`` — comma-separated filter
    * ``project_id`` — scope to a project
    """
    if max_depth < 1 or max_depth > 5:
        raise ApiError(400, "bad_request", "max_depth must be between 1 and 5")
    if direction not in ("outgoing", "incoming", "both"):
        raise ApiError(400, "bad_request", "direction must be outgoing, incoming, or both")

    rt_list = _parse_relation_types(relation_types)

    result = engine.neighborhood(
        source_id=memory_id,
        max_depth=max_depth,
        direction=direction,
        relation_types=rt_list,
        project_id=project_id,
    )

    if not result.success:
        raise ApiError(500, "internal_error", result.error or "Neighborhood query failed")

    return envelope(
        {
            "source_memory_id": str(memory_id),
            "max_depth": max_depth,
            "nodes": result.neighbor_nodes,
            "edges": result.neighbor_edges,
            "total_nodes": len(result.neighbor_nodes),
            "total_edges": len(result.neighbor_edges),
        },
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════
# GET /graph/edges — list edges
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/edges", response_model=dict)
def list_edges_endpoint(
    memory_id: UUID | None = None,
    relation_type: str | None = None,
    relation_status: str | None = None,
    project_id: UUID | None = None,
    pagination: PaginationParams = Depends(),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """List graph edges (memory relations) with denormalized node titles."""
    items, total = list_graph_edges(
        db,
        memory_id=memory_id,
        relation_type=relation_type,
        relation_status=relation_status,
        project_id=project_id,
        page=pagination.page,
        page_size=pagination.page_size,
    )

    edges = [GraphEdgeRead.model_validate(item) for item in items]
    pi = _page_info(total, pagination.page, pagination.page_size)
    data = GraphEdgeListResponse(items=edges, page_info=pi)

    return envelope(data.model_dump(mode="json"), request_id=context.request_id, correlation_id=context.correlation_id)


# ═══════════════════════════════════════════════════════════════════════════
# GET /graph/edges/{relation_id} — edge detail
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/edges/{relation_id}", response_model=dict)
def get_edge_endpoint(
    relation_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Get a single graph edge by relation_id, with denormalized node titles."""
    edge = get_graph_edge(db, relation_id=relation_id)
    if edge is None:
        raise ApiError(404, "bad_request", f"graph edge (relation) {relation_id} not found")

    data = GraphEdgeRead.model_validate(edge)
    return envelope(data.model_dump(mode="json"), request_id=context.request_id, correlation_id=context.correlation_id)


# ═══════════════════════════════════════════════════════════════════════════
# POST /graph/query — graph traversal query (via GraphEngine + SQL fallbacks)
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/query", response_model=dict)
def graph_query_endpoint(
    body: GraphQueryRequest,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    engine: GraphEngine = Depends(_get_graph_engine),
) -> dict:
    """Execute a graph traversal query via GraphEngine.

    Supported modes:
    * ``neighborhood`` — N-hop BFS via GraphEngine.neighborhood
    * ``shortest_path`` — via GraphEngine.shortest_path
    * ``connected`` — via GraphEngine.connected
    * ``subgraph`` — SQL-based induced subgraph
    * ``ppr`` — Personalized PageRank via GraphEngine.ppr
    * ``community`` — Louvain community detection via GraphEngine.community
    """
    # ── neighborhood ─────────────────────────────────────────────────────
    if body.mode == GraphQueryMode.neighborhood:
        if body.source_memory_id is None:
            raise ApiError(400, "bad_request", "source_memory_id is required for neighborhood queries")

        result = engine.neighborhood(
            source_id=body.source_memory_id,
            max_depth=body.max_depth,
            direction=body.direction.value,
            relation_types=body.relation_types,
            project_id=None,
        )
        if not result.success:
            raise ApiError(500, "internal_error", result.error or "Neighborhood query failed")

        return envelope(
            {
                "source_memory_id": str(body.source_memory_id),
                "max_depth": body.max_depth,
                "nodes": result.neighbor_nodes,
                "edges": result.neighbor_edges,
                "total_nodes": len(result.neighbor_nodes),
                "total_edges": len(result.neighbor_edges),
            },
            request_id=context.request_id,
            correlation_id=context.correlation_id,
        )

    # ── shortest_path ─────────────────────────────────────────────────────
    elif body.mode == GraphQueryMode.shortest_path:
        if body.source_memory_id is None or body.target_memory_id is None:
            raise ApiError(400, "bad_request",
                          "source_memory_id and target_memory_id are required for shortest_path queries")

        result = engine.shortest_path(
            source_id=body.source_memory_id,
            target_id=body.target_memory_id,
            algorithm="bfs",
            max_depth=body.max_depth,
            max_paths=10,
            relation_types=body.relation_types,
        )
        if not result.success:
            raise ApiError(500, "internal_error", result.error or "Shortest path query failed")

        return envelope(
            {
                "source_memory_id": str(body.source_memory_id),
                "target_memory_id": str(body.target_memory_id),
                "max_depth": body.max_depth,
                "paths": result.paths,
                "total_paths": result.path_count,
                "shortest_path_length": result.shortest_distance,
            },
            request_id=context.request_id,
            correlation_id=context.correlation_id,
        )

    # ── connected ─────────────────────────────────────────────────────────
    elif body.mode == GraphQueryMode.connected:
        if body.source_memory_id is None or body.target_memory_id is None:
            raise ApiError(400, "bad_request",
                          "source_memory_id and target_memory_id are required for connected queries")

        result = engine.connected(
            source_id=body.source_memory_id,
            target_id=body.target_memory_id,
            max_depth=body.max_depth,
            relation_types=body.relation_types,
        )
        if not result.success:
            raise ApiError(500, "internal_error", result.error or "Connected query failed")

        return envelope(
            {
                "source_memory_id": str(body.source_memory_id),
                "target_memory_id": str(body.target_memory_id),
                "connected": result.connected,
                "distance": result.shortest_distance,
            },
            request_id=context.request_id,
            correlation_id=context.correlation_id,
        )

    # ── subgraph ──────────────────────────────────────────────────────────
    elif body.mode == GraphQueryMode.subgraph:
        if body.node_ids is None or len(body.node_ids) == 0:
            raise ApiError(400, "bad_request", "node_ids is required for subgraph queries")
        if len(body.node_ids) > 100:
            raise ApiError(400, "bad_request", "subgraph queries are limited to 100 nodes")

        result = _sql_subgraph(
            db,
            node_ids=body.node_ids,
            relation_types=body.relation_types,
        )
        data = GraphSubgraphResponse.model_validate(result)
        return envelope(data.model_dump(mode="json"), request_id=context.request_id, correlation_id=context.correlation_id)

    # ── ppr ───────────────────────────────────────────────────────────────
    elif body.mode == "ppr":  # type: ignore[comparison-overlap]
        if body.node_ids is None or len(body.node_ids) == 0:
            raise ApiError(400, "bad_request", "node_ids (as seeds) is required for PPR queries")

        seeds = {UUID(str(nid)): 1.0 for nid in body.node_ids}
        result = engine.ppr(
            seed_nodes=seeds,
            top_k=min(body.max_depth * 5, 50),
            alpha=0.85,
            relation_types=body.relation_types,
            direction=body.direction.value,
        )
        return envelope(result.to_dict(), request_id=context.request_id, correlation_id=context.correlation_id)

    # ── community ─────────────────────────────────────────────────────────
    elif body.mode == "community":  # type: ignore[comparison-overlap]
        result = engine.community(relation_types=body.relation_types)
        return envelope(result.to_dict(), request_id=context.request_id, correlation_id=context.correlation_id)

    else:
        raise ApiError(400, "bad_request", f"Unknown query mode: {body.mode}")


# ═══════════════════════════════════════════════════════════════════════════
# GET /graph/summary — graph statistics
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/summary", response_model=dict)
def get_summary_endpoint(
    project_id: UUID | None = None,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Get aggregated graph statistics (node/edge counts, degree distribution, etc.)."""
    summary = get_graph_summary(db, project_id=project_id)
    data = GraphSummary.model_validate(summary)
    return envelope(data.model_dump(mode="json"), request_id=context.request_id, correlation_id=context.correlation_id)


# ═══════════════════════════════════════════════════════════════════════════
# POST /graph/nodes — create a graph node
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/nodes", response_model=dict, status_code=201)
def create_node_endpoint(
    body: GraphNodeCreate,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Create a new graph node (wraps memory creation in draft status)."""
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "Idempotency-Key header required")

    mem_create = MemoryCreate(
        project_id=body.project_id,
        title=body.title,
        memory_text=body.memory_text,
        sensitivity_level=body.sensitivity_level,
        canonical_key=body.canonical_key,
        node_type=body.node_type,
    )

    try:
        result = create_memory(db, context, payload=mem_create)
    except ValueError as e:
        raise ApiError(400, "bad_request", str(e))

    node = get_graph_node(db, memory_id=result.memory_id)
    if node is None:
        raise ApiError(500, "internal_error", "Node creation failed")

    data = GraphNodeRead.model_validate(node)
    return envelope(data.model_dump(mode="json"), request_id=context.request_id, correlation_id=context.correlation_id)


# ═══════════════════════════════════════════════════════════════════════════
# PATCH /graph/nodes/{memory_id} — update node attributes
# ═══════════════════════════════════════════════════════════════════════════

@router.patch("/nodes/{memory_id}", response_model=dict)
def update_node_endpoint(
    memory_id: UUID,
    body: GraphNodeUpdate,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Update mutable attributes of a graph node (title, sensitivity_level)."""
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "Idempotency-Key header required")

    existing = get_memory(db, memory_id)
    if existing is None:
        raise ApiError(404, "bad_request", f"graph node {memory_id} not found")

    mem_update = MemoryUpdate(
        title=body.title,
        memory_text=None,
        sensitivity_level=body.sensitivity_level,
        node_type=body.node_type,
    )

    try:
        result = update_memory(db, context, memory_id=memory_id, payload=mem_update)
    except ValueError as e:
        raise ApiError(400, "bad_request", str(e))

    node = get_graph_node(db, memory_id=result.memory_id)
    if node is None:
        raise ApiError(500, "internal_error", "Node update failed")

    data = GraphNodeRead.model_validate(node)
    return envelope(data.model_dump(mode="json"), request_id=context.request_id, correlation_id=context.correlation_id)


# ═══════════════════════════════════════════════════════════════════════════
# POST /graph/edges — create a graph edge
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/edges", response_model=dict, status_code=201)
def create_edge_endpoint(
    body: GraphEdgeCreate,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Create a new graph edge (wraps memory relation creation).

    UNIQUE(from_memory_id, to_memory_id, relation_type) prevents duplicate edges.
    CHECK(from_memory_id <> to_memory_id) prevents self-loops.
    """
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "Idempotency-Key header required")

    rel_create = MemoryRelationCreate(
        from_memory_id=body.from_memory_id,
        to_memory_id=body.to_memory_id,
        relation_type=body.relation_type,
        reason=body.reason,
        metadata_json=body.metadata_json,
    )

    try:
        result = create_memory_relation(db, context, payload=rel_create)
    except ValueError as e:
        raise ApiError(400, "bad_request", str(e))
    except IntegrityError:
        raise ApiError(
            409, "idempotency_conflict",
            "A relation of this type already exists between these two nodes"
            " or self-reference is not allowed.",
        )

    edge = get_graph_edge(db, relation_id=result.memory_relation_id)
    if edge is None:
        raise ApiError(500, "internal_error", "Edge creation failed")

    data = GraphEdgeRead.model_validate(edge)
    return envelope(data.model_dump(mode="json"), request_id=context.request_id, correlation_id=context.correlation_id)


# ═══════════════════════════════════════════════════════════════════════════
# DELETE /graph/edges/{relation_id} — cancel an edge
# ═══════════════════════════════════════════════════════════════════════════

@router.delete("/edges/{relation_id}", response_model=dict)
def delete_edge_endpoint(
    relation_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Cancel (soft-delete) a graph edge (active → cancelled)."""
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "Idempotency-Key header required")

    existing = get_memory_relation(db, relation_id)
    if existing is None:
        raise ApiError(404, "bad_request", f"graph edge (relation) {relation_id} not found")
    if existing.relation_status != "active":
        raise ApiError(
            409, "bad_request",
            f"graph edge {relation_id} is '{existing.relation_status}', only 'active' can be cancelled",
        )

    try:
        result = cancel_relation(db, context, memory_relation_id=relation_id)
    except ValueError as e:
        raise ApiError(400, "bad_request", str(e))

    return envelope(
        {
            "cancelled": True,
            "memory_relation_id": str(relation_id),
            "relation_status": result.relation_status,
        },
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════
# POST /graph/ppr — Personalized PageRank (via GraphEngine)
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/ppr", response_model=dict)
def ppr_endpoint(
    seed_nodes: dict[str, float],
    top_k: int = 12,
    alpha: float = 0.85,
    project_id: UUID | None = None,
    relation_types: str | None = None,
    direction: str = "both",
    engine: GraphEngine = Depends(_get_graph_engine),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Run Personalized PageRank traversal from seed nodes via GraphEngine.

    Request body (JSON)::

        {
            "seed_nodes": {
                "<memory_id>": 0.8,
                "<memory_id>": 0.5
            },
            "top_k": 12,
            "alpha": 0.85,
            "project_id": "<uuid | null>"
        }

    Returns discovered node IDs with PPR scores sorted descending.
    """
    if not seed_nodes:
        raise ApiError(400, "bad_request", "seed_nodes is required")

    rt_list = _parse_relation_types(relation_types)

    try:
        seeds = {UUID(k): v for k, v in seed_nodes.items()}
    except ValueError:
        raise ApiError(400, "bad_request", "Invalid UUID in seed_nodes")

    result = engine.ppr(
        seed_nodes=seeds,
        top_k=min(top_k, 100),
        alpha=alpha,
        project_id=project_id,
        relation_types=rt_list,
        direction=direction,
    )

    return envelope(
        {
            "success": result.success,
            "error": result.error,
            "elapsed_ms": result.elapsed_ms,
            "seed_count": result.ppr_seed_count,
            "discovered": result.ppr_discovered,
            "scores": {str(k): v for k, v in result.ppr_scores.items()},
        },
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════
# POST /graph/community — community detection (via GraphEngine)
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/community", response_model=dict)
def community_endpoint(
    algorithm: str = "louvain",
    resolution: float = 1.0,
    min_community_size: int = 2,
    project_id: UUID | None = None,
    relation_types: str | None = None,
    engine: GraphEngine = Depends(_get_graph_engine),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Detect communities in the memory graph via GraphEngine (Louvain / Girvan-Newman).

    Query parameters:
    * ``algorithm`` — ``"louvain"`` (default) or ``"girvan_newman"``
    * ``resolution`` — > 1.0 produces more/smaller communities
    * ``min_community_size`` — minimum nodes per community
    * ``project_id`` — scope to a project
    * ``relation_types`` — comma-separated edge type filter
    """
    rt_list = _parse_relation_types(relation_types)

    result = engine.community(
        algorithm=algorithm,
        resolution=resolution,
        min_community_size=min_community_size,
        project_id=project_id,
        relation_types=rt_list,
    )

    return envelope(
        {
            "success": result.success,
            "error": result.error,
            "elapsed_ms": result.elapsed_ms,
            "community_count": result.community_count,
            "modularity": result.modularity,
            "community_sizes": result.community_sizes,
            "communities": [[str(n) for n in c] for c in result.communities],
        },
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════
# POST /graph/analyze — full suite graph analysis (via GraphEngine)
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/analyze", response_model=dict)
def analyze_endpoint(
    seed_nodes: dict[str, float] | None = None,
    project_id: UUID | None = None,
    relation_types: str | None = None,
    engine: GraphEngine = Depends(_get_graph_engine),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Run a full suite of graph analyses (PPR + community detection).

    Optional ``seed_nodes`` triggers PPR analysis alongside community detection.
    """
    rt_list = _parse_relation_types(relation_types)

    seeds = None
    if seed_nodes:
        try:
            seeds = {UUID(k): v for k, v in seed_nodes.items()}
        except ValueError:
            raise ApiError(400, "bad_request", "Invalid UUID in seed_nodes")

    results = engine.analyze(
        seed_nodes=seeds,
        project_id=project_id,
        relation_types=rt_list,
    )

    return envelope(
        {name: r.to_dict() for name, r in results.items()},
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )
