"""GraphEngine — unified orchestrator for memory-graph analytics.

Provides a single entry-point for PPR, community detection, shortest paths,
and neighborhood queries against the memory graph (``memories`` nodes +
``memory_relations`` edges).  Internally powered by NetworkX.

Usage::

    from mneme.graph_engine import GraphEngine

    engine = GraphEngine(db_session)
    ppr_result = engine.ppr(seed_nodes={...})
    communities = engine.community()
    paths = engine.shortest_path(src_id, tgt_id)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from uuid import UUID

import networkx as nx
from sqlalchemy.orm import Session

from mneme.graph_engine.nx_builder import build_nx_graph
from mneme.graph_engine.community import (
    CommunityConfig,
    CommunityResult,
    community_detect_louvain,
    community_detect_girvan_newman,
)
from mneme.graph_engine.paths import (
    PathConfig,
    PathResult,
    find_shortest_paths as _find_bfs_paths,
    find_shortest_path_dijkstra,
)
from mneme.graph_engine.ppr import ppr_search, ppr_batch_search

logger = logging.getLogger(__name__)


class GraphQueryMode(str, Enum):
    ppr = "ppr"
    community = "community"
    shortest_path = "shortest_path"
    neighborhood = "neighborhood"
    connected = "connected"


@dataclass
class GraphAnalysisResult:
    """Unified result container for all graph engine operations."""

    mode: GraphQueryMode
    success: bool = True
    error: str | None = None
    elapsed_ms: float = 0.0

    # PPR fields
    ppr_scores: dict[UUID, float] = field(default_factory=dict)
    ppr_seed_count: int = 0
    ppr_discovered: int = 0

    # Community detection fields
    communities: list[list[UUID]] = field(default_factory=list)
    community_count: int = 0
    modularity: float = 0.0
    community_sizes: list[int] = field(default_factory=list)

    # Shortest path fields
    paths: list[dict[str, Any]] = field(default_factory=list)
    shortest_distance: int | None = None
    path_count: int = 0

    # Neighborhood / connectivity fields
    neighbor_nodes: list[dict[str, Any]] = field(default_factory=list)
    neighbor_edges: list[dict[str, Any]] = field(default_factory=list)
    connected: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "success": self.success,
            "error": self.error,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "ppr_scores": {str(k): v for k, v in self.ppr_scores.items()},
            "ppr_seed_count": self.ppr_seed_count,
            "ppr_discovered": self.ppr_discovered,
            "communities": [[str(n) for n in c] for c in self.communities],
            "community_count": self.community_count,
            "modularity": round(self.modularity, 4),
            "community_sizes": self.community_sizes,
            "paths": self.paths,
            "shortest_distance": self.shortest_distance,
            "path_count": self.path_count,
            "neighbor_nodes": self.neighbor_nodes,
            "neighbor_edges": self.neighbor_edges,
            "connected": self.connected,
        }


# ═══════════════════════════════════════════════════════════════════════════
# GraphEngine
# ═══════════════════════════════════════════════════════════════════════════


class GraphEngine:
    """Orchestrates all graph analytics on the memory graph.

    Parameters
    ----------
    db : Session
        Active SQLAlchemy database session.

    Usage::

        engine = GraphEngine(db)
        result = engine.ppr(seed_nodes={UUID(...): 0.8})
        communities = engine.community()
        paths = engine.shortest_path(source, target)
    """

    def __init__(self, db: Session) -> None:
        self.db = db

    # ── PPR (Personalized PageRank) ──────────────────────────────────────────

    def ppr(
        self,
        *,
        seed_nodes: dict[UUID, float],
        top_k: int = 12,
        alpha: float = 0.85,
        max_iterations: int = 100,
        convergence_eps: float = 1e-6,
        project_id: UUID | None = None,
        relation_types: list[str] | None = None,
        direction: str = "both",
    ) -> GraphAnalysisResult:
        """Run Personalized PageRank from seed nodes.

        Uses NetworkX's pagerank with personalization vector.
        """
        t0 = time.monotonic()
        result = GraphAnalysisResult(mode=GraphQueryMode.ppr)

        try:
            scores = ppr_search(
                self.db,
                seed_memory_ids=seed_nodes,
                top_k=top_k,
                alpha=alpha,
                max_iterations=max_iterations,
                convergence_eps=convergence_eps,
                project_id=project_id,
                relation_types=relation_types,
                direction=direction,
            )
            result.ppr_scores = scores
            result.ppr_seed_count = len(seed_nodes)
            result.ppr_discovered = len(scores)
            result.success = True
        except Exception as exc:
            logger.exception("PPR failed: %s", exc)
            result.success = False
            result.error = str(exc)
        finally:
            result.elapsed_ms = (time.monotonic() - t0) * 1000.0

        return result

    def ppr_batch(
        self,
        *,
        seed_sets: list[dict[UUID, float]],
        top_k: int = 12,
        alpha: float = 0.85,
        project_id: UUID | None = None,
    ) -> list[dict[UUID, float]]:
        """Run PPR for multiple seed sets sharing one graph load."""
        return ppr_batch_search(
            self.db,
            seed_sets=seed_sets,
            top_k=top_k,
            alpha=alpha,
            project_id=project_id,
        )

    # ── Community Detection ──────────────────────────────────────────────────

    def community(
        self,
        *,
        algorithm: str = "louvain",
        resolution: float = 1.0,
        min_community_size: int = 2,
        project_id: UUID | None = None,
        relation_types: list[str] | None = None,
    ) -> GraphAnalysisResult:
        """Detect communities in the memory graph.

        Parameters
        ----------
        algorithm : str
            ``"louvain"`` (default) or ``"girvan_newman"``.
        resolution : float
            Louvain resolution parameter.
        min_community_size : int
            Minimum nodes per community.
        project_id : UUID | None
        relation_types : list[str] | None
        """
        t0 = time.monotonic()
        result = GraphAnalysisResult(mode=GraphQueryMode.community)

        try:
            config = CommunityConfig(
                algorithm=algorithm,
                resolution=resolution,
                min_community_size=min_community_size,
            )

            if algorithm == "girvan_newman":
                cres = community_detect_girvan_newman(
                    self.db,
                    config=config,
                    project_id=project_id,
                    relation_types=relation_types,
                )
            else:
                cres = community_detect_louvain(
                    self.db,
                    config=config,
                    project_id=project_id,
                    relation_types=relation_types,
                )

            result.communities = cres.communities
            result.community_count = cres.community_count
            result.modularity = cres.modularity
            result.community_sizes = [len(c) for c in cres.communities]
            result.success = True
        except Exception as exc:
            logger.exception("Community detection failed: %s", exc)
            result.success = False
            result.error = str(exc)
        finally:
            result.elapsed_ms = (time.monotonic() - t0) * 1000.0

        return result

    # ── Shortest Paths ───────────────────────────────────────────────────────

    def shortest_path(
        self,
        *,
        source_id: UUID,
        target_id: UUID,
        algorithm: str = "bfs",
        max_depth: int = 10,
        max_paths: int = 10,
        weighted: bool = False,
        project_id: UUID | None = None,
        relation_types: list[str] | None = None,
    ) -> GraphAnalysisResult:
        """Find shortest paths between two nodes using NetworkX.

        Parameters
        ----------
        source_id : UUID
        target_id : UUID
        algorithm : str
            ``"bfs"`` (unweighted) or ``"dijkstra"`` (weighted).
        max_depth : int
        max_paths : int
        weighted : bool
        project_id : UUID | None
        relation_types : list[str] | None
        """
        t0 = time.monotonic()
        result = GraphAnalysisResult(mode=GraphQueryMode.shortest_path)

        try:
            if weighted or algorithm == "dijkstra":
                config = PathConfig(
                    algorithm="dijkstra",
                    max_depth=max_depth,
                    max_paths=max_paths,
                    weighted=True,
                )
                path_result = find_shortest_path_dijkstra(
                    self.db,
                    source_id=source_id,
                    target_id=target_id,
                    config=config,
                    project_id=project_id,
                    relation_types=relation_types,
                )
                result.paths = [p.to_dict() for p in path_result.paths]
                result.shortest_distance = (
                    int(path_result.distance)
                    if path_result.distance is not None
                    else None
                )
                result.path_count = path_result.path_count
            else:
                path_result = _find_bfs_paths(
                    self.db,
                    source_memory_id=source_id,
                    target_memory_id=target_id,
                    max_depth=max_depth,
                    max_paths=max_paths,
                    relation_types=relation_types,
                    project_id=project_id,
                )
                result.paths = [p.to_dict() for p in path_result.paths]
                result.shortest_distance = path_result.distance
                result.path_count = path_result.path_count

            result.success = True
        except Exception as exc:
            logger.exception("Shortest path failed: %s", exc)
            result.success = False
            result.error = str(exc)
        finally:
            result.elapsed_ms = (time.monotonic() - t0) * 1000.0

        return result

    # ── Neighborhood ─────────────────────────────────────────────────────────

    def neighborhood(
        self,
        *,
        source_id: UUID,
        max_depth: int = 3,
        direction: str = "both",
        relation_types: list[str] | None = None,
        project_id: UUID | None = None,
    ) -> GraphAnalysisResult:
        """Get the N-hop neighborhood around a node via NetworkX BFS.

        Parameters
        ----------
        source_id : UUID
        max_depth : int
        direction : str
            ``"outgoing"``, ``"incoming"``, or ``"both"``.
        relation_types : list[str] | None
        project_id : UUID | None
        """
        t0 = time.monotonic()
        result = GraphAnalysisResult(mode=GraphQueryMode.neighborhood)

        try:
            G, node_data = build_nx_graph(
                self.db,
                project_id=project_id,
                relation_types=relation_types,
                directed=(direction != "both"),
            )

            if source_id not in G:
                result.success = True
                result.elapsed_ms = (time.monotonic() - t0) * 1000.0
                return result

            # BFS neighborhood via single_source_shortest_path_length
            lengths = nx.single_source_shortest_path_length(
                G, source=source_id, cutoff=max_depth,
            )

            neighbor_nodes: list[dict[str, Any]] = []
            neighbor_edges: list[dict[str, Any]] = []
            seen_edges: set[tuple[UUID, UUID]] = set()

            for nid, depth in lengths.items():
                if nid == source_id:
                    continue
                nd = node_data.get(nid, {})
                neighbor_nodes.append({
                    "memory_id": str(nid),
                    "title": nd.get("title"),
                    "canonical_key": nd.get("canonical_key"),
                    "status": nd.get("status", "unknown"),
                    "depth": depth,
                })

            # Collect edges within the neighborhood
            for nid in lengths:
                if nid == source_id:
                    continue
                path = nx.shortest_path(G, source_id, nid)
                for i in range(len(path) - 1):
                    a, b = path[i], path[i + 1]
                    edge_key = (
                        (a, b) if str(a) < str(b) else (b, a)
                    )
                    if edge_key in seen_edges:
                        continue
                    seen_edges.add(edge_key)
                    edge_data = G.get_edge_data(a, b) or {}
                    neighbor_edges.append({
                        "memory_relation_id": str(edge_data.get("relation_id", "")),
                        "from_memory_id": str(a),
                        "to_memory_id": str(b),
                        "relation_type": edge_data.get("relation_type", ""),
                        "relation_status": "active",
                    })

            result.neighbor_nodes = neighbor_nodes
            result.neighbor_edges = neighbor_edges
            result.success = True
        except Exception as exc:
            logger.exception("Neighborhood query failed: %s", exc)
            result.success = False
            result.error = str(exc)
        finally:
            result.elapsed_ms = (time.monotonic() - t0) * 1000.0

        return result

    # ── Connectivity Check ───────────────────────────────────────────────────

    def connected(
        self,
        *,
        source_id: UUID,
        target_id: UUID,
        max_depth: int = 10,
        relation_types: list[str] | None = None,
        project_id: UUID | None = None,
    ) -> GraphAnalysisResult:
        """Check if two nodes are connected and get the distance via NetworkX."""
        t0 = time.monotonic()
        result = GraphAnalysisResult(mode=GraphQueryMode.connected)

        try:
            G, node_data = build_nx_graph(
                self.db,
                project_id=project_id,
                relation_types=relation_types,
                directed=False,
            )

            if source_id not in G or target_id not in G:
                result.connected = False
                result.success = True
                return result

            try:
                sp = nx.shortest_path(G, source=source_id, target=target_id)
                dist = len(sp) - 1
                if dist <= max_depth:
                    result.connected = True
                    result.shortest_distance = dist
                else:
                    result.connected = False
            except nx.NetworkXNoPath:
                result.connected = False

            result.success = True
        except Exception as exc:
            logger.exception("Connectivity check failed: %s", exc)
            result.success = False
            result.error = str(exc)
        finally:
            result.elapsed_ms = (time.monotonic() - t0) * 1000.0

        return result

    # ── Full Analysis ────────────────────────────────────────────────────────

    def analyze(
        self,
        *,
        seed_nodes: dict[UUID, float] | None = None,
        project_id: UUID | None = None,
        relation_types: list[str] | None = None,
    ) -> dict[str, GraphAnalysisResult]:
        """Run a full suite of graph analyses (PPR + community detection)."""
        results: dict[str, GraphAnalysisResult] = {}

        if seed_nodes:
            results["ppr"] = self.ppr(
                seed_nodes=seed_nodes,
                project_id=project_id,
                relation_types=relation_types,
            )

        results["community"] = self.community(
            project_id=project_id,
            relation_types=relation_types,
        )

        return results
