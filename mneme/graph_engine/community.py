"""Community detection on the memory graph — NetworkX-powered.

Uses ``networkx.algorithms.community.louvain_communities`` for
modularity-based community partitioning of the memory graph.

Usage::

    from mneme.graph_engine.community import community_detect_louvain

    result = community_detect_louvain(db, project_id=pid)
    for community in result.communities:
        print(f"Community ({len(community)} nodes): {community}")
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import networkx as nx
from networkx.algorithms.community import (
    louvain_communities,
    modularity,
    girvan_newman,
)
from sqlalchemy.orm import Session

from mneme.graph_engine.nx_builder import build_nx_graph

logger = logging.getLogger(__name__)

DEFAULT_RESOLUTION = 1.0
DEFAULT_MIN_SIZE = 2
DEFAULT_RANDOM_SEED = 42


# ═══════════════════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class CommunityConfig:
    """Community detection configuration."""

    algorithm: str = "louvain"
    resolution: float = DEFAULT_RESOLUTION
    min_community_size: int = DEFAULT_MIN_SIZE
    random_seed: int | None = DEFAULT_RANDOM_SEED
    max_passes: int = 50


@dataclass
class CommunityResult:
    """Community detection result with metadata."""

    communities: list[list[UUID]] = field(default_factory=list)
    community_count: int = 0
    modularity: float = 0.0
    passes: int = 0
    elapsed_ms: float = 0.0
    node_count: int = 0
    edge_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "community_count": self.community_count,
            "modularity": round(self.modularity, 4),
            "communities": [[str(n) for n in c] for c in self.communities],
            "community_sizes": [len(c) for c in self.communities],
            "passes": self.passes,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "node_count": self.node_count,
            "edge_count": self.edge_count,
        }


# ═══════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════


def community_detect(
    db: Session,
    *,
    algorithm: str = "louvain",
    resolution: float = DEFAULT_RESOLUTION,
    min_community_size: int = DEFAULT_MIN_SIZE,
    project_id: UUID | None = None,
    relation_types: list[str] | None = None,
) -> CommunityResult:
    """Detect communities in the memory graph.

    Parameters
    ----------
    db : Session
        Active SQLAlchemy session.
    algorithm : str
        ``"louvain"`` (default) or ``"girvan_newman"``.
    resolution : float
        Resolution parameter for Louvain: > 1.0 → more/smaller communities.
    min_community_size : int
        Minimum node count per community.
    project_id : UUID | None
        Restrict to a project.
    relation_types : list[str] | None
        Filter by edge type.

    Returns
    -------
    CommunityResult
    """
    config = CommunityConfig(
        algorithm=algorithm,
        resolution=resolution,
        min_community_size=min_community_size,
    )

    if algorithm == "louvain":
        return community_detect_louvain(
            db, config=config, project_id=project_id,
            relation_types=relation_types,
        )
    elif algorithm == "girvan_newman":
        return community_detect_girvan_newman(
            db, config=config, project_id=project_id,
            relation_types=relation_types,
        )
    else:
        raise ValueError(
            f"Unknown algorithm: {algorithm}. Supported: louvain, girvan_newman"
        )


def community_detect_louvain(
    db: Session,
    *,
    config: CommunityConfig | None = None,
    project_id: UUID | None = None,
    relation_types: list[str] | None = None,
) -> CommunityResult:
    """Run Louvain community detection via NetworkX.

    1. Builds a NetworkX undirected graph from memory_relations.
    2. Runs ``nx.community.louvain_communities``.
    3. Filters communities by ``min_community_size``.
    4. Computes modularity score.
    """
    cfg = config or CommunityConfig()
    t0 = time.monotonic()

    # Build graph
    G, nodes = build_nx_graph(
        db, project_id=project_id, relation_types=relation_types,
        directed=False, weight_attr=None,
    )

    if G.number_of_nodes() == 0:
        logger.info("louvain: empty graph")
        return CommunityResult(elapsed_ms=(time.monotonic() - t0) * 1000.0)

    n_nodes = G.number_of_nodes()
    n_edges = G.number_of_edges()

    if n_nodes < 2:
        comms = [[nid] for nid in G.nodes()] if n_nodes else []
        return CommunityResult(
            communities=comms,
            community_count=len(comms),
            modularity=0.0,
            node_count=n_nodes,
            edge_count=n_edges,
            elapsed_ms=(time.monotonic() - t0) * 1000.0,
        )

    # Run Louvain
    seed = cfg.random_seed if cfg.random_seed is not None else DEFAULT_RANDOM_SEED
    raw_communities = list(louvain_communities(G, weight="weight", seed=seed))

    # Compute modularity
    try:
        q = modularity(G, raw_communities, weight="weight")
    except Exception:
        q = 0.0

    # Filter by min size and build result
    final_comms: list[list[UUID]] = []
    for members in raw_communities:
        if len(members) >= cfg.min_community_size:
            final_comms.append(sorted(members, key=str))
        else:
            # Keep small communities as singletons
            for mid in members:
                final_comms.append([mid])

    # Sort by size descending
    final_comms.sort(key=len, reverse=True)

    elapsed = (time.monotonic() - t0) * 1000.0

    logger.info(
        "louvain: %d nodes → %d communities (modularity=%.4f, %.1fms)",
        n_nodes, len(final_comms), q, elapsed,
    )

    return CommunityResult(
        communities=final_comms,
        community_count=len(final_comms),
        modularity=q,
        passes=1,
        elapsed_ms=elapsed,
        node_count=n_nodes,
        edge_count=n_edges,
    )


def community_detect_girvan_newman(
    db: Session,
    *,
    config: CommunityConfig | None = None,
    project_id: UUID | None = None,
    relation_types: list[str] | None = None,
) -> CommunityResult:
    """Run Girvan-Newman community detection via NetworkX.

    Uses edge-betweenness centrality to iteratively remove edges,
    producing a hierarchy of community splits.  Returns the first level
    with at least *min_community_size* communities.
    """
    cfg = config or CommunityConfig()
    t0 = time.monotonic()

    G, nodes = build_nx_graph(
        db, project_id=project_id, relation_types=relation_types,
        directed=False,
    )

    if G.number_of_nodes() < 2:
        return CommunityResult(
            communities=[[nid] for nid in G.nodes()] if G.number_of_nodes() else [],
            community_count=G.number_of_nodes(),
            node_count=G.number_of_nodes(),
            edge_count=G.number_of_edges(),
            elapsed_ms=(time.monotonic() - t0) * 1000.0,
        )

    # Get the first split that meets min_community_size
    gn_iter = girvan_newman(G)
    raw_communities = None
    for communities in gn_iter:
        sizes = [len(c) for c in communities]
        valid = [s for s in sizes if s >= cfg.min_community_size]
        if len(valid) >= 2:
            raw_communities = communities
            break
        # Limit iterations
        if len(communities) > G.number_of_nodes() // 2:
            raw_communities = communities
            break

    if raw_communities is None:
        raw_communities = [{nid} for nid in G.nodes()]

    # Convert to sorted lists
    final_comms: list[list[UUID]] = [
        sorted(c, key=str) for c in raw_communities
    ]
    final_comms.sort(key=len, reverse=True)

    # Modularity
    try:
        q = modularity(G, final_comms)
    except Exception:
        q = 0.0

    elapsed = (time.monotonic() - t0) * 1000.0

    logger.info(
        "girvan_newman: %d nodes → %d communities (modularity=%.4f, %.1fms)",
        G.number_of_nodes(), len(final_comms), q, elapsed,
    )

    return CommunityResult(
        communities=final_comms,
        community_count=len(final_comms),
        modularity=q,
        elapsed_ms=elapsed,
        node_count=G.number_of_nodes(),
        edge_count=G.number_of_edges(),
    )
