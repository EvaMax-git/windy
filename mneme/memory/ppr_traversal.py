"""PPR (Personalized PageRank) graph traversal for memory search recall.

Integrates with the memory graph (``memories`` → ``memory_relations``) to
boost search recall by traversing the graph from FTS/ILIKE seed nodes using
Personalized PageRank.

Algorithm
---------
1. Seed nodes are derived from text search results (FTS/ILIKE).
2. An undirected (or directed) graph is loaded from active memory relations.
3. Iterative PPR is run with teleportation probability ``α``:
   - With probability ``α``, follow a random outgoing edge.
   - With probability ``1-α``, teleport back to a seed (proportional to seed weight).
4. After convergence, top-k nodes by PPR score are returned.
5. These graph-discovered nodes are merged with direct search results.

Usage
-----
.. code-block:: python

    from mneme.memory.ppr_traversal import ppr_search

    results = ppr_search(
        db,
        seed_memory_ids={mid1: 0.8, mid2: 0.5},
        top_k=15,
        alpha=0.85,
        max_iterations=50,
        project_id=pid,
    )
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from mneme.db.graph import _load_full_graph

logger = logging.getLogger(__name__)

# ── Defaults ──────────────────────────────────────────────────────────────

DEFAULT_ALPHA = 0.85       # Teleport probability
DEFAULT_MAX_ITER = 50      # Max PPR iterations
DEFAULT_TOP_K = 12         # Max PPR-discovered nodes to return
DEFAULT_CONVERGENCE_EPS = 1e-6


# ═══════════════════════════════════════════════════════════════════════════
# PPR Core
# ═══════════════════════════════════════════════════════════════════════════

def ppr_search(
    db: Session,
    *,
    seed_memory_ids: dict[UUID, float] | None = None,
    top_k: int = DEFAULT_TOP_K,
    alpha: float = DEFAULT_ALPHA,
    max_iterations: int = DEFAULT_MAX_ITER,
    convergence_eps: float = DEFAULT_CONVERGENCE_EPS,
    project_id: UUID | None = None,
    relation_types: list[str] | None = None,
    direction: str = "both",
) -> dict[UUID, float]:
    """Run Personalized PageRank on the memory graph seeded by query-relevant nodes.

    Parameters
    ----------
    db : Session
        Active SQLAlchemy session.
    seed_memory_ids : dict[UUID, float] | None
        Map of seed node IDs → initial weights (e.g., FTS rank scores).
        Weights are normalized internally.
    top_k : int
        Maximum number of graph-discovered nodes to return (excluding seeds).
    alpha : float
        Teleport probability (0 < α < 1). Higher = more exploration,
        lower = stick closer to seeds.
    max_iterations : int
        Maximum PPR iterations for convergence.
    convergence_eps : float
        Stop when max score change < ε between iterations.
    project_id : UUID | None
        Scope graph traversal to a project.
    relation_types : list[str] | None
        Filter edges by relation type (e.g. ['similar', 'references']).
    direction : str
        Traversal direction: ``"outgoing"``, ``"incoming"``, or ``"both"``.

    Returns
    -------
    dict[UUID, float]
        Map of discovered memory IDs → PPR score, sorted descending,
        excluding seed nodes.
    """
    if not seed_memory_ids:
        return {}

    # 1. Load the full graph
    nodes, outgoing_adj, incoming_adj = _load_full_graph(
        db, project_id=project_id, relation_types=relation_types,
    )

    if not nodes:
        logger.warning("ppr_traversal: empty graph, returning no results")
        return {}

    # 2. Build adjacency list based on direction
    adj: dict[UUID, list[UUID]] = defaultdict(list)
    all_node_ids: set[UUID] = set(nodes.keys())

    for from_id, edges in outgoing_adj.items():
        for e in edges:
            to_id = e["to_memory_id"]
            if isinstance(to_id, str):
                to_id = UUID(to_id)
            if to_id not in all_node_ids:
                continue
            if direction in ("outgoing", "both"):
                adj[from_id].append(to_id)
            if direction in ("incoming", "both"):
                adj[to_id].append(from_id)

    # Ensure all nodes have an adjacency entry
    for nid in all_node_ids:
        if nid not in adj:
            adj[nid] = []

    # 3. Normalize seed weights → personalization vector
    seeds_present: dict[UUID, float] = {}
    total_seed_weight = 0.0
    for seed_id, weight in seed_memory_ids.items():
        sid = seed_id if isinstance(seed_id, UUID) else UUID(str(seed_id))
        if sid in all_node_ids:
            seeds_present[sid] = max(weight, 0.01)
            total_seed_weight += seeds_present[sid]

    if not seeds_present:
        logger.warning("ppr_traversal: no valid seeds in graph")
        return {}

    # Normalize to sum=1
    personalization: dict[UUID, float] = {}
    for sid, w in seeds_present.items():
        personalization[sid] = w / total_seed_weight

    # 4. Initialize PPR scores
    node_list = list(all_node_ids)
    ppr: dict[UUID, float] = {nid: 0.0 for nid in node_list}
    for sid in seeds_present:
        ppr[sid] = personalization.get(sid, 1.0 / len(seeds_present))

    # 5. Iterative PPR (power iteration)
    for iteration in range(max_iterations):
        new_ppr: dict[UUID, float] = {nid: 0.0 for nid in node_list}
        max_change = 0.0

        for node_id in node_list:
            # Teleport component: (1-α) × personalization
            teleport = (1.0 - alpha) * personalization.get(node_id, 0.0)

            # Walk component: α × Σ(ppr[neighbor] / out_degree[neighbor])
            walk_sum = 0.0
            for neighbor in adj.get(node_id, []):
                if neighbor not in ppr:
                    continue
                neighbor_deg = len(adj.get(neighbor, []))
                if neighbor_deg > 0:
                    walk_sum += ppr[neighbor] / neighbor_deg

            walk = alpha * walk_sum
            new_ppr[node_id] = teleport + walk

            change = abs(new_ppr[node_id] - ppr[node_id])
            if change > max_change:
                max_change = change

        ppr = new_ppr

        if max_change < convergence_eps:
            logger.debug("ppr_traversal: converged after %d iterations", iteration + 1)
            break
    else:
        logger.debug("ppr_traversal: reached max iterations (%d)", max_iterations)

    # 6. Collect top-k (excluding seeds)
    seed_set = set(seeds_present.keys())
    non_seed_scores: list[tuple[UUID, float]] = [
        (nid, score) for nid, score in ppr.items()
        if nid not in seed_set and score > 0.0
    ]
    non_seed_scores.sort(key=lambda x: x[1], reverse=True)

    result: dict[UUID, float] = {}
    for nid, score in non_seed_scores[:top_k]:
        result[nid] = round(score, 6)

    logger.info(
        "ppr_traversal: %d seeds → %d discovered nodes (top_k=%d, α=%.2f)",
        len(seeds_present), len(result), top_k, alpha,
    )

    return result


# ═══════════════════════════════════════════════════════════════════════════
# Fetch node details for PPR-discovered nodes
# ═══════════════════════════════════════════════════════════════════════════

_FETCH_NODES_BY_IDS = text("""
    SELECT
        m.memory_id,
        m.title,
        m.canonical_key,
        m.memory_text,
        m.sensitivity_level,
        m.status,
        m.project_id,
        m.current_version,
        m.node_type,
        m.updated_at
    FROM memories m
    WHERE m.memory_id = ANY(:mids)
      AND m.status IN ('active', 'draft')
""")


def fetch_ppr_node_details(
    db: Session,
    ppr_scores: dict[UUID, float],
) -> list[dict[str, Any]]:
    """Fetch full memory details for PPR-discovered nodes.

    Parameters
    ----------
    db : Session
        Active SQLAlchemy session.
    ppr_scores : dict[UUID, float]
        Map from ``ppr_search`` output.

    Returns
    -------
    list[dict]
        Memory rows augmented with ``ppr_score``.
    """
    if not ppr_scores:
        return []

    mids = list(ppr_scores.keys())
    rows = db.execute(_FETCH_NODES_BY_IDS, {"mids": mids}).mappings().all()

    results: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        mid = item["memory_id"]
        if isinstance(mid, str):
            mid = UUID(mid)
        item["ppr_score"] = ppr_scores.get(mid, 0.0)
        results.append(item)

    results.sort(key=lambda r: r.get("ppr_score", 0.0), reverse=True)
    return results


# ═══════════════════════════════════════════════════════════════════════════
# Convenience: run PPR and fetch details
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class PprRecallResult:
    """Aggregated PPR search recall result."""

    seed_count: int = 0
    ppr_discovered_count: int = 0
    ppr_scores: dict[UUID, float] = field(default_factory=dict)
    node_details: list[dict[str, Any]] = field(default_factory=list)
    elapsed_ms: float = 0.0


def run_ppr_recall(
    db: Session,
    *,
    seed_memory_ids: dict[UUID, float],
    top_k: int = DEFAULT_TOP_K,
    alpha: float = DEFAULT_ALPHA,
    project_id: UUID | None = None,
) -> PprRecallResult:
    """Run the full PPR recall pipeline: score → fetch details.

    Parameters
    ----------
    db : Session
        Active SQLAlchemy session.
    seed_memory_ids : dict[UUID, float]
        Seed nodes with weights (typically from FTS rank from 0-1).
    top_k : int
        Max PPR-discovered nodes.
    alpha : float
        PPR teleport probability.
    project_id : UUID | None
        Scope to a project.

    Returns
    -------
    PprRecallResult
    """
    t0 = time.monotonic()
    result = PprRecallResult(seed_count=len(seed_memory_ids))

    if not seed_memory_ids:
        return result

    ppr_scores = ppr_search(
        db,
        seed_memory_ids=seed_memory_ids,
        top_k=top_k,
        alpha=alpha,
        project_id=project_id,
    )

    result.ppr_scores = ppr_scores
    result.ppr_discovered_count = len(ppr_scores)
    result.node_details = fetch_ppr_node_details(db, ppr_scores)
    result.elapsed_ms = (time.monotonic() - t0) * 1000.0

    return result
