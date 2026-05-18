"""GraphEngine — PPR, community detection, shortest paths on the memory graph.

Powered by NetworkX over the ``memories`` + ``memory_relations`` tables.

Public API
----------
* ``GraphEngine`` — orchestrator class for all graph analytics
* ``ppr_search`` — Personalized PageRank traversal
* ``community_detect`` — Louvain / Girvan-Newman community detection
* ``find_shortest_paths`` — BFS + Dijkstra shortest paths
* ``build_nx_graph`` — build a raw NetworkX graph from memory_relations
* ``GraphAnalysisResult`` — unified result container
"""

from mneme.graph_engine.engine import GraphEngine, GraphAnalysisResult, GraphQueryMode
from mneme.graph_engine.nx_builder import build_nx_graph
from mneme.graph_engine.ppr import ppr_search, ppr_batch_search, PprConfig, PprResult
from mneme.graph_engine.community import (
    community_detect,
    community_detect_louvain,
    community_detect_girvan_newman,
    CommunityResult,
    CommunityConfig,
)
from mneme.graph_engine.paths import (
    find_shortest_paths,
    find_shortest_path_dijkstra,
    find_all_pairs_shortest_paths,
    PathResult,
    PathConfig,
    PathStep,
)

__all__ = [
    # ── Engine ──
    "GraphEngine",
    "GraphAnalysisResult",
    "GraphQueryMode",
    # ── Graph builder ──
    "build_nx_graph",
    # ── PPR ──
    "ppr_search",
    "ppr_batch_search",
    "PprConfig",
    "PprResult",
    # ── Community Detection ──
    "community_detect",
    "community_detect_louvain",
    "community_detect_girvan_newman",
    "CommunityResult",
    "CommunityConfig",
    # ── Shortest Paths ──
    "find_shortest_paths",
    "find_shortest_path_dijkstra",
    "find_all_pairs_shortest_paths",
    "PathResult",
    "PathConfig",
    "PathStep",
]
