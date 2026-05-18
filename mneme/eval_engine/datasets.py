"""Benchmark datasets for evaluating memory graph algorithms.

Each dataset generates test items from the live data in the memory graph,
or can be constructed from synthetic/sampled data if the graph is sparse.

Dataset types
-------------
* ``graph_connectivity`` — Tests shortest-path and connectivity accuracy.
  Generates known-connected and known-disconnected node pairs from the graph.
* ``ppr_recall`` — Tests PPR recall accuracy.
  Takes node neighborhoods and verifies PPR can rediscover them from
  partial seed sets.
* ``community_detection`` — Tests community detection quality.
  Generates ground-truth communities from known subgraphs.

Usage
-----
.. code-block:: python

    from mneme.eval_engine.datasets import get_dataset

    ds = get_dataset("ppr_recall")
    ds.generate(db, project_id=pid)
    for item in ds.get_test_items():
        print(item["seeds"], "→", item["expected_ids"])
"""

from __future__ import annotations

import logging
import random
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from mneme.db.graph import _load_full_graph

logger = logging.getLogger(__name__)

RANDOM_SEED = 42


# ═══════════════════════════════════════════════════════════════════════════
# Abstract base
# ═══════════════════════════════════════════════════════════════════════════


class BenchmarkDataset(ABC):
    """Abstract base for benchmark datasets.

    Subclasses must implement ``generate()`` and ``get_test_items()``.
    """

    name: str = "base"
    description: str = ""
    max_items: int = 50

    def __init__(self, max_items: int | None = None) -> None:
        self.max_items = max_items or self.max_items
        self._items: list[dict[str, Any]] = []
        self._generated = False

    @abstractmethod
    def generate(
        self,
        db: Session,
        *,
        project_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Generate test items from the database."""

    def get_test_items(self) -> list[dict[str, Any]]:
        """Return the generated test items."""
        if not self._generated:
            logger.warning("%s: generate() not called, returning empty list", self.name)
        return self._items

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "item_count": len(self._items),
            "generated": self._generated,
        }


# ═══════════════════════════════════════════════════════════════════════════
# Graph Connectivity Dataset
# ═══════════════════════════════════════════════════════════════════════════


class GraphConnectivityDataset(BenchmarkDataset):
    """Tests shortest-path and connectivity accuracy.

    Strategy:
    1. Load the full memory graph.
    2. For each component, select random node pairs within the same
       component (known-connected) and across different components
       (known-disconnected).
    3. For connected pairs, record the BFS distance as ground truth.
    """

    name = "graph_connectivity"
    description = "Shortest-path & connectivity accuracy benchmark"
    max_items = 50

    def generate(
        self,
        db: Session,
        *,
        project_id: UUID | None = None,
        max_depth: int = 5,
    ) -> None:
        """Generate connectivity test pairs.

        Parameters
        ----------
        db : Session
        project_id : UUID | None
        max_depth : int
            BFS depth limit for ground-truth distance computation.
        """
        random.seed(RANDOM_SEED)

        # Load graph
        nodes, outgoing_adj, incoming_adj = _load_full_graph(
            db, project_id=project_id,
        )

        if len(nodes) < 4:
            logger.warning("connectivity dataset: too few nodes (%d)", len(nodes))
            self._items = []
            self._generated = True
            return

        # Build undirected adjacency
        adj: dict[UUID, list[UUID]] = defaultdict(list)
        for from_id, edges in outgoing_adj.items():
            for e in edges:
                to_id = UUID(str(e["to_memory_id"]))
                adj[from_id].append(to_id)
                adj[to_id].append(from_id)

        # Find connected components
        components = _find_components(adj, list(nodes.keys()))

        # Generate connected pairs (within-component)
        connected_items: list[dict[str, Any]] = []
        for comp in components:
            if len(comp) < 2:
                continue
            comp_list = list(comp)
            random.shuffle(comp_list)
            pairs = min(len(comp_list) // 2, 5)
            for i in range(pairs):
                src = comp_list[i]
                tgt = comp_list[(i + 1) % len(comp_list)]
                # Compute BFS distance
                dist = _bfs_distance(adj, src, tgt, max_depth)
                if dist is not None and dist > 0:
                    connected_items.append({
                        "source_id": src,
                        "target_id": tgt,
                        "connected": True,
                        "expected_distance": dist,
                    })

        # Generate disconnected pairs (cross-component)
        disconnected_items: list[dict[str, Any]] = []
        if len(components) >= 2:
            comp_sizes = sorted(enumerate(components), key=lambda x: len(x[1]), reverse=True)
            for i in range(min(len(comp_sizes) - 1, 10)):
                idx_a, comp_a = comp_sizes[i]
                idx_b, comp_b = comp_sizes[i + 1]
                src = random.choice(list(comp_a))
                tgt = random.choice(list(comp_b))
                disconnected_items.append({
                    "source_id": src,
                    "target_id": tgt,
                    "connected": False,
                    "expected_distance": None,
                })

        # Balance and trim
        items = connected_items[:self.max_items // 2] + disconnected_items[:self.max_items // 2]
        random.shuffle(items)
        self._items = items[:self.max_items]
        self._generated = True

        logger.info(
            "connectivity dataset: %d items (%d connected, %d disconnected)",
            len(self._items),
            sum(1 for i in self._items if i["connected"]),
            sum(1 for i in self._items if not i["connected"]),
        )


# ═══════════════════════════════════════════════════════════════════════════
# PPR Recall Dataset
# ═══════════════════════════════════════════════════════════════════════════


class PprRecallDataset(BenchmarkDataset):
    """Tests PPR recall: can PPR rediscover neighbors from partial seeds?

    Strategy:
    1. For a random node, get its N-hop neighborhood.
    2. Use a subset of the neighborhood as seeds.
    3. The full neighborhood becomes the expected recall set.
    """

    name = "ppr_recall"
    description = "PPR recall against known graph neighborhoods"
    max_items = 30

    def generate(
        self,
        db: Session,
        *,
        project_id: UUID | None = None,
        neighborhood_depth: int = 2,
        seed_fraction: float = 0.4,
    ) -> None:
        """Generate PPR recall test items.

        Parameters
        ----------
        db : Session
        project_id : UUID | None
        neighborhood_depth : int
            Depth for ground-truth neighborhood extraction.
        seed_fraction : float
            Fraction of neighbors to use as seeds (rest = expected recall).
        """
        random.seed(RANDOM_SEED)

        nodes, outgoing_adj, incoming_adj = _load_full_graph(
            db, project_id=project_id,
        )

        if len(nodes) < 5:
            logger.warning("ppr_recall dataset: too few nodes (%d)", len(nodes))
            self._items = []
            self._generated = True
            return

        # Build undirected adjacency
        adj: dict[UUID, list[UUID]] = defaultdict(list)
        for from_id, edges in outgoing_adj.items():
            for e in edges:
                to_id = UUID(str(e["to_memory_id"]))
                adj[from_id].append(to_id)
                adj[to_id].append(from_id)

        node_list = list(nodes.keys())
        random.shuffle(node_list)

        items: list[dict[str, Any]] = []
        for center_id in node_list[:self.max_items * 2]:
            # Get N-hop neighborhood
            neighborhood = _get_nhop_neighborhood(adj, center_id, neighborhood_depth)

            if len(neighborhood) < 3:
                continue

            neighbors = list(neighborhood)
            random.shuffle(neighbors)

            # Split into seeds and expected
            n_seeds = max(1, int(len(neighbors) * seed_fraction))
            seed_ids = neighbors[:n_seeds]
            expected_ids = neighbors[n_seeds:]

            if len(expected_ids) == 0:
                continue

            # Assign simple weights based on distance
            seeds = {sid: 1.0 - (i * 0.1) for i, sid in enumerate(seed_ids)}

            items.append({
                "center_id": center_id,
                "seeds": seeds,
                "expected_ids": list(expected_ids),
            })

            if len(items) >= self.max_items:
                break

        self._items = items
        self._generated = True

        logger.info("ppr_recall dataset: %d items", len(self._items))


# ═══════════════════════════════════════════════════════════════════════════
# Community Detection Dataset
# ═══════════════════════════════════════════════════════════════════════════


class CommunityDetectionDataset(BenchmarkDataset):
    """Tests community detection quality.

    Strategy:
    1. Find naturally isolated subgraphs (components) as ground truth.
    2. Detect communities via Louvain.
    3. Compare overlap with known components.
    """

    name = "community_detection"
    description = "Community detection quality benchmark"
    max_items = 1  # Single holistic evaluation

    def generate(
        self,
        db: Session,
        *,
        project_id: UUID | None = None,
    ) -> None:
        """Generate community ground truth from connected components.

        Parameters
        ----------
        db : Session
        project_id : UUID | None
        """
        nodes, outgoing_adj, incoming_adj = _load_full_graph(
            db, project_id=project_id,
        )

        if len(nodes) < 4:
            logger.warning("community dataset: too few nodes (%d)", len(nodes))
            self._items = []
            self._generated = True
            return

        # Build undirected adjacency
        adj: dict[UUID, list[UUID]] = defaultdict(list)
        for from_id, edges in outgoing_adj.items():
            for e in edges:
                to_id = UUID(str(e["to_memory_id"]))
                adj[from_id].append(to_id)
                adj[to_id].append(from_id)

        node_list = list(nodes.keys())
        for nid in node_list:
            if nid not in adj:
                adj[nid] = []

        # Connected components as ground truth
        ground_truth = _find_components(adj, node_list)
        ground_truth = [list(c) for c in ground_truth if len(c) >= 2]

        self._items = [{
            "ground_truth_communities": ground_truth,
            "node_count": len(nodes),
            "component_count": len(ground_truth),
        }]
        self._generated = True

        logger.info(
            "community dataset: %d nodes, %d ground-truth components",
            len(nodes), len(ground_truth),
        )


# ═══════════════════════════════════════════════════════════════════════════
# Dataset registry
# ═══════════════════════════════════════════════════════════════════════════

_DATASET_REGISTRY: dict[str, type[BenchmarkDataset]] = {
    "graph_connectivity": GraphConnectivityDataset,
    "ppr_recall": PprRecallDataset,
    "community_detection": CommunityDetectionDataset,
}


def get_dataset(name: str, **kwargs: Any) -> BenchmarkDataset:
    """Get a benchmark dataset by name.

    Parameters
    ----------
    name : str
        Dataset name: ``"graph_connectivity"``, ``"ppr_recall"``,
        ``"community_detection"``.
    **kwargs
        Passed to the dataset constructor (e.g., ``max_items``).

    Returns
    -------
    BenchmarkDataset
    """
    if name not in _DATASET_REGISTRY:
        raise ValueError(
            f"Unknown dataset '{name}'. Available: {list(_DATASET_REGISTRY.keys())}"
        )
    return _DATASET_REGISTRY[name](**kwargs)


def list_datasets() -> list[str]:
    """Return available dataset names."""
    return list(_DATASET_REGISTRY.keys())


def register_dataset(name: str, cls: type[BenchmarkDataset]) -> None:
    """Register a custom dataset type for extensibility."""
    _DATASET_REGISTRY[name] = cls
    logger.info("Registered dataset '%s'", name)


# ═══════════════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════════════


def _find_components(
    adj: dict[UUID, list[UUID]],
    nodes: list[UUID],
) -> list[set[UUID]]:
    """Find connected components via BFS."""
    visited: set[UUID] = set()
    components: list[set[UUID]] = []

    for node in nodes:
        if node in visited:
            continue
        # BFS
        comp: set[UUID] = set()
        queue = deque([node])
        visited.add(node)
        while queue:
            current = queue.popleft()
            comp.add(current)
            for neighbor in adj.get(current, []):
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(neighbor)
        components.append(comp)

    return components


def _bfs_distance(
    adj: dict[UUID, list[UUID]],
    source: UUID,
    target: UUID,
    max_depth: int = 5,
) -> int | None:
    """Compute BFS distance between two nodes."""
    if source == target:
        return 0

    visited: dict[UUID, int] = {source: 0}
    queue: deque[UUID] = deque([source])

    while queue:
        current = queue.popleft()
        dist = visited[current]
        if dist >= max_depth:
            continue
        for neighbor in adj.get(current, []):
            if neighbor == target:
                return dist + 1
            if neighbor not in visited:
                visited[neighbor] = dist + 1
                queue.append(neighbor)

    return None


def _get_nhop_neighborhood(
    adj: dict[UUID, list[UUID]],
    center: UUID,
    depth: int,
) -> set[UUID]:
    """Get all nodes within N hops of center (excluding center)."""
    visited: set[UUID] = set()
    queue = deque([(center, 0)])
    visited.add(center)

    while queue:
        current, d = queue.popleft()
        if d >= depth:
            continue
        for neighbor in adj.get(current, []):
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append((neighbor, d + 1))

    visited.discard(center)
    return visited
