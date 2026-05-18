"""P7 Graph API schemas — memory graph nodes and edges.

Graph model
-----------
* **Node** — a ``memory`` row, identified by ``memory_id``.
* **Edge** — a ``memory_relation`` row, linking two nodes with a typed relationship.
* **Graph** — the collective topology of nodes connected by edges.

Relation types (from ``memory_relations`` CHECK constraint):
* ``conflicts_with``, ``supersedes``, ``merged_into``, ``duplicates``, ``supports``
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import Field

from mneme.schemas.common import ApiSchema, PaginatedData


# ── Graph traversal direction ────────────────────────────────────────────

class TraversalDirection(str, Enum):
    """Direction of graph traversal from a source node."""
    outgoing = "outgoing"   # follow from_memory_id → to_memory_id
    incoming = "incoming"   # follow to_memory_id → from_memory_id
    both = "both"           # both directions


class GraphQueryMode(str, Enum):
    """Type of graph query to perform."""
    neighborhood = "neighborhood"  # N-hop neighborhood
    shortest_path = "shortest_path"  # shortest path between two nodes
    connected = "connected"       # check if two nodes are connected
    subgraph = "subgraph"         # subgraph around a set of nodes
    ppr = "ppr"                  # Personalized PageRank from seed nodes
    community = "community"       # Louvain community detection


# ── Graph node ────────────────────────────────────────────────────────────

class GraphNodeRead(ApiSchema):
    """A memory represented as a graph node, including edge counts."""

    memory_id: UUID
    project_id: UUID | None = None
    canonical_key: str
    title: str | None = None
    memory_text: str
    current_version: int
    sensitivity_level: str
    status: str
    node_type: str | None = None
    quality_score: float | None = None
    search_weight: float = 1.0
    activated_at: datetime | None = None
    expired_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    # Graph-specific metadata
    out_degree: int = 0
    in_degree: int = 0
    total_relations: int = 0


class GraphNodeListResponse(PaginatedData[GraphNodeRead]):
    """Paginated list of graph nodes."""
    pass


class GraphNodeCreate(ApiSchema):
    """Create a new graph node (wraps memory creation)."""

    project_id: UUID
    title: str | None = Field(default=None, max_length=240)
    memory_text: str = Field(min_length=1)
    sensitivity_level: str = "private"
    canonical_key: str | None = Field(
        default=None, max_length=160,
    )
    node_type: str | None = Field(
        default=None,
        description="Graph node type: episode, fact, reflection, or concept.",
    )


class GraphNodeUpdate(ApiSchema):
    """Update mutable attributes of a graph node."""

    title: str | None = Field(default=None, max_length=240)
    sensitivity_level: str | None = None
    node_type: str | None = Field(
        default=None,
        description="Graph node type: episode, fact, reflection, or concept.",
    )


# ── Graph edge ────────────────────────────────────────────────────────────

class GraphEdgeRead(ApiSchema):
    """A memory_relation represented as a graph edge."""

    memory_relation_id: UUID
    from_memory_id: UUID
    to_memory_id: UUID
    relation_type: str
    relation_status: str
    reason: str | None = None
    metadata_json: Any = Field(default_factory=dict)
    created_at: datetime | None = None
    # Denormalized for convenience
    from_title: str | None = None
    to_title: str | None = None
    from_canonical_key: str | None = None
    to_canonical_key: str | None = None


class GraphEdgeListResponse(PaginatedData[GraphEdgeRead]):
    """Paginated list of graph edges."""
    pass


class GraphEdgeCreate(ApiSchema):
    """Create a new edge (relation) between two graph nodes."""

    from_memory_id: UUID
    to_memory_id: UUID
    relation_type: str = Field(
        description="Relation type: conflicts_with, supersedes, merged_into, duplicates, "
        "supports, similar, causal, temporal, contradicts, references"
    )
    reason: str | None = None
    metadata_json: dict[str, Any] = Field(default_factory=dict)


# ── Graph queries ─────────────────────────────────────────────────────────

class GraphQueryRequest(ApiSchema):
    """Request body for graph traversal queries."""

    mode: GraphQueryMode = GraphQueryMode.neighborhood
    source_memory_id: UUID | None = Field(
        default=None,
        description="Source node for neighborhood / shortest_path queries",
    )
    target_memory_id: UUID | None = Field(
        default=None,
        description="Target node for shortest_path / connected queries",
    )
    node_ids: list[UUID] | None = Field(
        default=None,
        description="Set of node IDs for subgraph queries",
    )
    max_depth: int = Field(
        default=3, ge=1, le=10,
        description="Maximum traversal depth for neighborhood queries",
    )
    relation_types: list[str] | None = Field(
        default=None,
        description="Filter by relation types",
    )
    direction: TraversalDirection = TraversalDirection.both


class GraphNeighborhoodNode(ApiSchema):
    """A node in a graph neighborhood, with depth information."""

    memory_id: UUID
    title: str | None = None
    canonical_key: str | None = None
    status: str
    depth: int = 0
    via_relation_type: str | None = None
    via_relation_id: UUID | None = None


class GraphNeighborhoodResponse(ApiSchema):
    """Result of a neighborhood query."""

    source_memory_id: UUID
    max_depth: int
    nodes: list[GraphNeighborhoodNode] = Field(default_factory=list)
    edges: list[GraphEdgeRead] = Field(default_factory=list)
    total_nodes: int = 0
    total_edges: int = 0


class GraphPathNode(ApiSchema):
    """A node along a graph path."""

    memory_id: UUID
    title: str | None = None
    canonical_key: str | None = None
    step: int = 0


class GraphPathEdge(ApiSchema):
    """An edge along a graph path."""

    relation_id: UUID
    from_memory_id: UUID
    to_memory_id: UUID
    relation_type: str
    step: int = 0


class GraphPathResponse(ApiSchema):
    """A single path between two graph nodes."""

    source_memory_id: UUID
    target_memory_id: UUID
    path_length: int = 0
    nodes: list[GraphPathNode] = Field(default_factory=list)
    edges: list[GraphPathEdge] = Field(default_factory=list)


class GraphPathsResponse(ApiSchema):
    """All paths found between two graph nodes (up to max_depth)."""

    source_memory_id: UUID
    target_memory_id: UUID
    max_depth: int
    paths: list[GraphPathResponse] = Field(default_factory=list)
    total_paths: int = 0
    shortest_path_length: int | None = None


class GraphConnectedResponse(ApiSchema):
    """Result of a connectivity check between two nodes."""

    source_memory_id: UUID
    target_memory_id: UUID
    connected: bool
    distance: int | None = None


class GraphSubgraphResponse(ApiSchema):
    """Subgraph induced by a set of nodes."""

    node_ids: list[UUID]
    nodes: list[GraphNodeRead] = Field(default_factory=list)
    edges: list[GraphEdgeRead] = Field(default_factory=list)
    total_nodes: int = 0
    total_edges: int = 0


class GraphSummary(ApiSchema):
    """Aggregated graph statistics."""

    total_nodes: int = 0
    total_edges: int = 0
    active_nodes: int = 0
    active_edges: int = 0
    relation_type_counts: dict[str, int] = Field(default_factory=dict)
    isolated_nodes: int = 0
    max_degree: int = 0
    avg_degree: float = 0.0
    project_id: UUID | None = None
