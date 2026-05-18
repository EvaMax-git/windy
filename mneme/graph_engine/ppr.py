"""Personalized PageRank (PPR) on the memory graph — NetworkX-powered.

Uses ``networkx.algorithms.link_analysis.pagerank_alg`` (numpy backend)
for efficient iterative PPR with personalization vector.

Usage::

    from mneme.graph_engine.ppr import ppr_search

    scores = ppr_search(db, seed_memory_ids={mid1: 0.8, mid2: 0.5}, top_k=15)
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import networkx as nx
from sqlalchemy.orm import Session

from mneme.graph_engine.nx_builder import build_nx_graph

logger = logging.getLogger(__name__)

DEFAULT_ALPHA = 0.85
DEFAULT_MAX_ITER = 100
DEFAULT_TOP_K = 12
DEFAULT_CONVERGENCE_TOL = 1e-6


# ═══════════════════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class PprConfig:
    """PPR algorithm configuration."""

    alpha: float = DEFAULT_ALPHA
    max_iterations: int = DEFAULT_MAX_ITER
    convergence_tol: float = DEFAULT_CONVERGENCE_TOL
    top_k: int = DEFAULT_TOP_K
    min_seed_weight: float = 0.01
    normalize_seeds: bool = True


@dataclass
class PprResult:
    """PPR run result with timing and convergence info."""

    scores: dict[UUID, float] = field(default_factory=dict)
    seeds_used: int = 0
    nodes_discovered: int = 0
    iterations: int = 0
    converged: bool = False
    max_delta_final: float = 0.0
    elapsed_ms: float = 0.0


# ═══════════════════════════════════════════════════════════════════════════
# Core PPR
# ═══════════════════════════════════════════════════════════════════════════


def ppr_search(
    db: Session,
    *,
    seed_memory_ids: dict[UUID, float],
    top_k: int = DEFAULT_TOP_K,
    alpha: float = DEFAULT_ALPHA,
    max_iterations: int = DEFAULT_MAX_ITER,
    convergence_eps: float = DEFAULT_CONVERGENCE_TOL,
    project_id: UUID | None = None,
    relation_types: list[str] | None = None,
    direction: str = "both",
) -> dict[UUID, float]:
    """Run Personalized PageRank on the memory graph via NetworkX.

    Parameters
    ----------
    db : Session
        Active SQLAlchemy session.
    seed_memory_ids : dict[UUID, float]
        Seed node IDs → initial relevance weights.
    top_k : int
        Max discovered nodes to return (excluding seeds).
    alpha : float
        Teleport probability, 0 < α < 1.
    max_iterations : int
        Max power iterations.
    convergence_eps : float
        Stop when max |score_{t+1} - score_t| < ε.
    project_id : UUID | None
        Scope to a project.
    relation_types : list[str] | None
        Filter by edge type.
    direction : str
        ``"outgoing"``, ``"incoming"``, or ``"both"``.

    Returns
    -------
    dict[UUID, float]
        Discovered node IDs → PPR scores, sorted descending, seeds excluded.
    """
    if not seed_memory_ids:
        return {}

    t0 = time.monotonic()

    # Build NetworkX graph
    G, nodes = build_nx_graph(
        db, project_id=project_id, relation_types=relation_types,
        directed=(direction != "both"),
    )

    if G.number_of_nodes() == 0:
        logger.debug("ppr: empty graph, no results")
        return {}

    # Build personalization dict: map node IDs → weight (normalized)
    personalization, seed_set = _build_personalization(seed_memory_ids, G, nodes)
    if not personalization:
        logger.debug("ppr: no valid seeds in graph")
        return {}

    # Run PPR via NetworkX numpy backend (falls back to Python)
    try:
        ppr_scores = _run_pagerank(
            G,
            personalization=personalization,
            alpha=alpha,
            max_iter=max_iterations,
            tol=convergence_eps,
        )
    except nx.PowerIterationFailedConvergence:
        logger.warning("ppr: did not converge within %d iterations", max_iterations)
        # Attempt to get partial results from last iteration
        try:
            ppr_scores = _run_pagerank(
                G,
                personalization=personalization,
                alpha=alpha,
                max_iter=max_iterations * 2,
                tol=convergence_eps * 10,
            )
        except Exception:
            logger.exception("ppr: fallback also failed")
            return {}

    # Extract top-k non-seed results
    ranked = [
        (nid, score)
        for nid, score in ppr_scores.items()
        if nid not in seed_set and score > 0
    ]
    ranked.sort(key=lambda x: x[1], reverse=True)

    result: dict[UUID, float] = {}
    for nid, score in ranked[:top_k]:
        result[nid] = round(score, 6)

    elapsed = (time.monotonic() - t0) * 1000.0
    logger.info(
        "ppr: %d seeds → %d discovered (%.1fms, α=%.2f)",
        len(personalization), len(result), elapsed, alpha,
    )

    return result


def ppr_search_full(
    db: Session,
    *,
    seed_memory_ids: dict[UUID, float],
    config: PprConfig | None = None,
    project_id: UUID | None = None,
    relation_types: list[str] | None = None,
    direction: str = "both",
) -> PprResult:
    """Run PPR and return the full :class:`PprResult` with metadata."""
    cfg = config or PprConfig()
    t0 = time.monotonic()

    if not seed_memory_ids:
        return PprResult()

    G, nodes = build_nx_graph(
        db, project_id=project_id, relation_types=relation_types,
    )
    if G.number_of_nodes() == 0:
        return PprResult(elapsed_ms=(time.monotonic() - t0) * 1000.0)

    personalization, seed_set = _build_personalization(seed_memory_ids, G, nodes)
    if not personalization:
        return PprResult(seeds_used=0, elapsed_ms=(time.monotonic() - t0) * 1000.0)

    converged = True
    try:
        ppr_scores = _run_pagerank(
            G,
            personalization=personalization,
            alpha=cfg.alpha,
            max_iter=cfg.max_iterations,
            tol=cfg.convergence_tol,
        )
    except nx.PowerIterationFailedConvergence:
        converged = False
        try:
            ppr_scores = _run_pagerank(
                G,
                personalization=personalization,
                alpha=cfg.alpha,
                max_iter=cfg.max_iterations,
                tol=cfg.convergence_tol * 10,
            )
        except Exception:
            return PprResult(
                seeds_used=len(personalization),
                converged=False,
                elapsed_ms=(time.monotonic() - t0) * 1000.0,
            )

    ranked = [
        (nid, score)
        for nid, score in ppr_scores.items()
        if nid not in seed_set and score > 0
    ]
    ranked.sort(key=lambda x: x[1], reverse=True)

    scores = {nid: round(score, 6) for nid, score in ranked[:cfg.top_k]}

    return PprResult(
        scores=scores,
        seeds_used=len(personalization),
        nodes_discovered=len(scores),
        iterations=cfg.max_iterations,
        converged=converged,
        elapsed_ms=(time.monotonic() - t0) * 1000.0,
    )


def ppr_batch_search(
    db: Session,
    *,
    seed_sets: list[dict[UUID, float]],
    top_k: int = DEFAULT_TOP_K,
    alpha: float = DEFAULT_ALPHA,
    project_id: UUID | None = None,
    relation_types: list[str] | None = None,
) -> list[dict[UUID, float]]:
    """Run PPR for multiple seed sets sharing one graph load."""
    if not seed_sets:
        return []

    t0 = time.monotonic()

    G, nodes = build_nx_graph(
        db, project_id=project_id, relation_types=relation_types,
    )
    if G.number_of_nodes() == 0:
        return [{} for _ in seed_sets]

    logger.info(
        "ppr_batch: loaded graph with %d nodes, running %d seed sets",
        G.number_of_nodes(), len(seed_sets),
    )

    results: list[dict[UUID, float]] = []
    for seeds in seed_sets:
        personalization, seed_set = _build_personalization(seeds, G, nodes)
        if not personalization:
            results.append({})
            continue

        try:
            ppr_scores = _run_pagerank(
                G,
                personalization=personalization,
                alpha=alpha,
                max_iter=50,
                tol=1e-6,
            )
        except Exception:
            results.append({})
            continue

        ranked = [
            (nid, score)
            for nid, score in ppr_scores.items()
            if nid not in seed_set and score > 0
        ]
        ranked.sort(key=lambda x: x[1], reverse=True)
        results.append({nid: round(s, 6) for nid, s in ranked[:top_k]})

    elapsed = (time.monotonic() - t0) * 1000.0
    logger.info("ppr_batch: %d sets completed in %.1fms", len(seed_sets), elapsed)

    return results


# ═══════════════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════════════


def _run_pagerank(
    G: nx.Graph,
    *,
    personalization: dict[UUID, float],
    alpha: float,
    max_iter: int,
    tol: float,
) -> dict[UUID, float]:
    """Run NetworkX pagerank with backend selection.

    Tries scipy → numpy → pure Python in that order.
    """
    # Attempt scipy backend
    try:
        import scipy  # noqa: F401
        from networkx.algorithms.link_analysis.pagerank_alg import (
            _pagerank_scipy,
        )
        return _pagerank_scipy(
            G, alpha=alpha, personalization=personalization,
            max_iter=max_iter, tol=tol, nstart=None, weight=None,
            dangling=None,
        )
    except ImportError:
        pass

    # Attempt numpy backend
    try:
        import numpy  # noqa: F401
        from networkx.algorithms.link_analysis.pagerank_alg import (
            _pagerank_numpy,
        )
        return _pagerank_numpy(
            G, alpha=alpha, personalization=personalization,
            max_iter=max_iter, tol=tol, nstart=None, weight=None,
            dangling=None,
        )
    except ImportError:
        pass

    # Pure Python fallback
    from networkx.algorithms.link_analysis.pagerank_alg import (
        _pagerank_python,
    )
    return _pagerank_python(
        G, alpha=alpha, personalization=personalization,
        max_iter=max_iter, tol=tol, nstart=None, weight=None,
        dangling=None,
    )


def _build_personalization(
    seeds: dict[UUID, float],
    G: nx.Graph,
    nodes: dict[UUID, dict],
) -> tuple[dict[UUID, float], set[UUID]]:
    """Build L1-normalized personalization dict for NetworkX pagerank.

    Returns (personalization, seed_set).
    Seeds not present in the graph are silently dropped.
    """
    valid: dict[UUID, float] = {}
    for sid, w in seeds.items():
        node_id = sid if isinstance(sid, UUID) else UUID(str(sid))
        if node_id in G:
            valid[node_id] = max(w, 0.01)

    if not valid:
        return {}, set()

    total = sum(valid.values())
    return {nid: w / total for nid, w in valid.items()}, set(valid.keys())
