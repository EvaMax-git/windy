"""Shortest path algorithms on the memory graph — NetworkX-powered.

Uses ``networkx.shortest_path``, ``networkx.all_shortest_paths``,
and ``networkx.dijkstra_path`` for finding connections between memory nodes.

Usage::

    from mneme.graph_engine.paths import find_shortest_path_dijkstra

    result = find_shortest_path_dijkstra(db, source_id=sid, target_id=tid)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import networkx as nx
from sqlalchemy.orm import Session

from mneme.graph_engine.nx_builder import build_nx_graph

logger = logging.getLogger(__name__)

DEFAULT_MAX_DEPTH = 10
DEFAULT_MAX_PATHS = 10


# ═══════════════════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class PathConfig:
    """Shortest path configuration."""

    algorithm: str = "bfs"
    max_depth: int = DEFAULT_MAX_DEPTH
    max_paths: int = DEFAULT_MAX_PATHS
    weighted: bool = False


@dataclass
class PathStep:
    """Single step in a path."""

    node_id: UUID
    title: str | None = None
    canonical_key: str | None = None
    step_index: int = 0
    relation_type: str | None = None
    relation_id: UUID | None = None
    edge_weight: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": str(self.node_id),
            "title": self.title,
            "canonical_key": self.canonical_key,
            "step_index": self.step_index,
            "relation_type": self.relation_type,
            "relation_id": str(self.relation_id) if self.relation_id else None,
            "edge_weight": self.edge_weight,
        }


@dataclass
class PathResult:
    """Result of a shortest path query."""

    source_id: UUID
    target_id: UUID
    paths: list[list[PathStep]] = field(default_factory=list)
    distance: float | None = None
    path_count: int = 0
    algorithm: str = "bfs"
    elapsed_ms: float = 0.0
    nodes_explored: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": str(self.source_id),
            "target_id": str(self.target_id),
            "paths": [[s.to_dict() for s in p] for p in self.paths],
            "distance": self.distance,
            "path_count": self.path_count,
            "algorithm": self.algorithm,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "nodes_explored": self.nodes_explored,
        }


# ═══════════════════════════════════════════════════════════════════════════
# Public API — BFS shortest paths (unweighted)
# ═══════════════════════════════════════════════════════════════════════════


def find_shortest_paths(
    db: Session,
    *,
    source_memory_id: UUID,
    target_memory_id: UUID,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_paths: int = DEFAULT_MAX_PATHS,
    relation_types: list[str] | None = None,
    project_id: UUID | None = None,
) -> PathResult:
    """Find all shortest paths between two nodes using BFS via NetworkX.

    Returns all paths of the shortest length (up to *max_paths*).
    Each edge weight = 1.

    Parameters
    ----------
    db : Session
    source_memory_id : UUID
    target_memory_id : UUID
    max_depth : int
        Maximum search depth (cutoff).
    max_paths : int
        Maximum number of paths to return.
    relation_types : list[str] | None
        Filter by edge type.
    project_id : UUID | None
        Restrict to a project.

    Returns
    -------
    PathResult
    """
    t0 = time.monotonic()

    source = _coerce(source_memory_id)
    target = _coerce(target_memory_id)

    if source == target:
        return PathResult(
            source_id=source,
            target_id=target,
            paths=[[PathStep(node_id=source, step_index=0)]],
            distance=0,
            path_count=1,
            algorithm="bfs",
            elapsed_ms=(time.monotonic() - t0) * 1000.0,
        )

    G, node_data = build_nx_graph(
        db, project_id=project_id, relation_types=relation_types,
        directed=False,
    )

    if source not in G or target not in G:
        return PathResult(
            source_id=source, target_id=target, algorithm="bfs",
            elapsed_ms=(time.monotonic() - t0) * 1000.0,
        )

    try:
        all_paths = list(
            nx.all_shortest_paths(G, source=source, target=target)
        )
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return PathResult(
            source_id=source, target_id=target, algorithm="bfs",
            nodes_explored=G.number_of_nodes(),
            elapsed_ms=(time.monotonic() - t0) * 1000.0,
        )

    # Filter by max_depth
    truncated: list[list[UUID]] = []
    for path in all_paths:
        if len(path) - 1 <= max_depth:
            truncated.append(path)

    truncated = truncated[:max_paths]

    # Build PathStep lists
    path_steps = _build_path_steps(truncated, node_data, G)

    distance = len(truncated[0]) - 1 if truncated else None

    return PathResult(
        source_id=source,
        target_id=target,
        paths=path_steps,
        distance=distance,
        path_count=len(truncated),
        algorithm="bfs",
        nodes_explored=G.number_of_nodes(),
        elapsed_ms=(time.monotonic() - t0) * 1000.0,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Public API — Dijkstra shortest path (weighted)
# ═══════════════════════════════════════════════════════════════════════════


def find_shortest_path_dijkstra(
    db: Session,
    *,
    source_id: UUID,
    target_id: UUID,
    config: PathConfig | None = None,
    project_id: UUID | None = None,
    relation_types: list[str] | None = None,
) -> PathResult:
    """Find the single shortest weighted path using Dijkstra via NetworkX.

    Edge weights are derived from relation metadata (see
    :func:`mneme.graph_engine.nx_builder._extract_weight`).
    """
    cfg = config or PathConfig(algorithm="dijkstra", weighted=True)
    t0 = time.monotonic()

    source = _coerce(source_id)
    target = _coerce(target_id)

    if source == target:
        return PathResult(
            source_id=source,
            target_id=target,
            paths=[[PathStep(node_id=source, step_index=0)]],
            distance=0,
            path_count=1,
            algorithm="dijkstra",
            elapsed_ms=(time.monotonic() - t0) * 1000.0,
        )

    # Build weighted graph
    G, node_data = build_nx_graph(
        db, project_id=project_id, relation_types=relation_types,
        directed=False, weight_attr="weight",
    )

    if source not in G or target not in G:
        return PathResult(
            source_id=source, target_id=target, algorithm="dijkstra",
            elapsed_ms=(time.monotonic() - t0) * 1000.0,
        )

    try:
        path_ids: list[UUID] = nx.dijkstra_path(
            G, source=source, target=target, weight="weight",
        )
        distance = nx.dijkstra_path_length(
            G, source=source, target=target, weight="weight",
        )
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return PathResult(
            source_id=source,
            target_id=target,
            algorithm="dijkstra",
            nodes_explored=G.number_of_nodes(),
            elapsed_ms=(time.monotonic() - t0) * 1000.0,
        )

    # Build PathSteps
    path_steps = _build_path_steps([path_ids], node_data, G)

    return PathResult(
        source_id=source,
        target_id=target,
        paths=path_steps,
        distance=round(distance, 4),
        path_count=1,
        algorithm="dijkstra",
        nodes_explored=G.number_of_nodes(),
        elapsed_ms=(time.monotonic() - t0) * 1000.0,
    )


# ═══════════════════════════════════════════════════════════════════════════
# All-pairs shortest paths
# ═══════════════════════════════════════════════════════════════════════════


def find_all_pairs_shortest_paths(
    db: Session,
    *,
    node_ids: list[UUID],
    weighted: bool = False,
    project_id: UUID | None = None,
    relation_types: list[str] | None = None,
) -> dict[tuple[UUID, UUID], PathResult]:
    """Compute shortest paths between all pairs in a node set via NetworkX."""
    t0 = time.monotonic()
    results: dict[tuple[UUID, UUID], PathResult] = {}

    node_set = set(_coerce(nid) for nid in node_ids)

    G, node_data = build_nx_graph(
        db, project_id=project_id, relation_types=relation_types,
        directed=False, weight_attr="weight" if weighted else None,
    )

    for src in node_set:
        if src not in G:
            continue
        for tgt in node_set:
            if src == tgt:
                continue
            key = (src, tgt)
            if key in results:
                continue
            if tgt not in G:
                continue

            try:
                if weighted:
                    path = nx.dijkstra_path(G, src, tgt, weight="weight")
                    dist = nx.dijkstra_path_length(G, src, tgt, weight="weight")
                else:
                    path = nx.shortest_path(G, src, tgt)
                    dist = len(path) - 1

                path_steps = _build_path_steps([path], node_data, G)
                result = PathResult(
                    source_id=src, target_id=tgt,
                    paths=path_steps,
                    distance=dist if weighted else int(dist),
                    path_count=1,
                    algorithm="dijkstra" if weighted else "bfs",
                )
                results[key] = result
                results[(tgt, src)] = result
            except (nx.NetworkXNoPath, nx.NodeNotFound):
                continue

    logger.info(
        "all_pairs: %d pairs computed in %.1fms",
        len(results), (time.monotonic() - t0) * 1000.0,
    )

    return results


# ═══════════════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════════════


def _coerce(value) -> UUID:
    """Coerce a value to UUID."""
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def _build_path_steps(
    paths: list[list[UUID]],
    node_data: dict[UUID, dict],
    G: nx.Graph,
) -> list[list[PathStep]]:
    """Convert raw UUID path lists to list-of-PathStep, annotated with edge info."""
    result: list[list[PathStep]] = []

    for path_ids in paths:
        steps: list[PathStep] = []
        for i, nid in enumerate(path_ids):
            nd = node_data.get(nid, {})
            step = PathStep(
                node_id=_coerce(nid),
                title=nd.get("title"),
                canonical_key=nd.get("canonical_key"),
                step_index=i,
            )

            # Annotate edge (from prev node)
            if i > 0:
                prev_id = path_ids[i - 1]
                edge_data = G.get_edge_data(prev_id, nid) or {}
                step.relation_type = edge_data.get("relation_type")
                step.relation_id = _coerce(edge_data.get("relation_id")) if edge_data.get("relation_id") else None
                step.edge_weight = float(edge_data.get("weight", 1.0))

            steps.append(step)

        result.append(steps)

    return result
