"""NetworkX graph builder from memory_relations.

Constructs a ``networkx.Graph`` (undirected) or ``networkx.DiGraph``
from the ``memories`` + ``memory_relations`` tables via
:func:`mneme.db.graph._load_full_graph`.

Usage::

    from mneme.graph_engine.nx_builder import build_nx_graph

    G, node_data = build_nx_graph(db, project_id=pid)
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import networkx as nx
from sqlalchemy.orm import Session

from mneme.db.graph import _load_full_graph

logger = logging.getLogger(__name__)


def build_nx_graph(
    db: Session,
    *,
    project_id: UUID | None = None,
    relation_types: list[str] | None = None,
    directed: bool = False,
    weight_attr: str | None = None,
) -> tuple[nx.Graph | nx.DiGraph, dict[UUID, dict]]:
    """Build a NetworkX graph from the active memory graph.

    Parameters
    ----------
    db : Session
        Active SQLAlchemy session.
    project_id : UUID | None
        Restrict to a project's subgraph.
    relation_types : list[str] | None
        Filter edges by type (e.g. ``['similar', 'supports']``).
    directed : bool
        If True, build a ``nx.DiGraph``; otherwise a ``nx.Graph``.
    weight_attr : str | None
        Edge attribute name to use as weight.  If ``None``, edges are
        unweighted.

    Returns
    -------
    (G, node_data)
        G : nx.Graph or nx.DiGraph
            Nodes have attributes from ``memories`` (title, canonical_key,
            status, etc.).  Edges have ``relation_type`` and ``relation_id``.
        node_data : dict[UUID, dict]
            Raw node data keyed by memory_id.
    """
    nodes, outgoing_adj, incoming_adj = _load_full_graph(
        db, project_id=project_id, relation_types=relation_types,
    )

    G = nx.DiGraph() if directed else nx.Graph()

    # ── Add nodes ──────────────────────────────────────────────────────────
    for node_id, node_info in nodes.items():
        G.add_node(
            node_id,
            title=node_info.get("title"),
            canonical_key=node_info.get("canonical_key"),
            status=node_info.get("status"),
            node_type=node_info.get("node_type"),
            project_id=node_info.get("project_id"),
            quality_score=node_info.get("quality_score"),
            search_weight=node_info.get("search_weight"),
        )

    # ── Add edges ──────────────────────────────────────────────────────────
    for from_id, edges in outgoing_adj.items():
        from_id = _to_uuid(from_id)
        for e in edges:
            to_id = _to_uuid(e.get("to_memory_id"))
            if from_id not in nodes or to_id not in nodes:
                continue

            edge_attrs: dict[str, Any] = {
                "relation_type": e.get("relation_type", "unknown"),
                "relation_id": e.get("memory_relation_id"),
            }
            if weight_attr:
                edge_attrs["weight"] = _extract_weight(e, weight_attr)

            if directed:
                G.add_edge(from_id, to_id, **edge_attrs)
            else:
                if G.has_edge(from_id, to_id):
                    if weight_attr and "weight" in G[from_id][to_id]:
                        existing = G[from_id][to_id]["weight"]
                        new_w = edge_attrs.get("weight", 1.0)
                        G[from_id][to_id]["weight"] = (existing + new_w) / 2.0
                else:
                    G.add_edge(from_id, to_id, **edge_attrs)

    logger.debug(
        "build_nx_graph: %d nodes, %d edges (directed=%s)",
        G.number_of_nodes(), G.number_of_edges(), directed,
    )

    return G, nodes


def _to_uuid(value) -> UUID:
    """Coerce a value to UUID."""
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        return UUID(value)
    raise TypeError(f"Cannot convert {type(value)} to UUID")


_RELATION_TYPE_WEIGHTS = {
    "conflicts_with": 2.0,
    "supersedes": 1.5,
    "duplicates": 0.5,
    "similar": 0.8,
    "references": 1.0,
    "supports": 1.0,
    "causal": 1.2,
    "temporal": 1.0,
    "merged_into": 1.0,
    "contradicts": 2.0,
    "extends": 1.0,
}


def _extract_weight(edge: dict, _attr: str) -> float:
    """Extract edge weight from a memory_relation edge dict.

    Priority: direct ``weight`` field → ``metadata_json.weight`` →
    relation-type heuristic → 1.0.
    """
    w = edge.get("weight")
    if isinstance(w, (int, float)) and w > 0:
        return float(w)

    meta = edge.get("metadata_json")
    if isinstance(meta, dict):
        w = meta.get("weight")
        if isinstance(w, (int, float)) and w > 0:
            return float(w)

    rel_type = edge.get("relation_type", "")
    return _RELATION_TYPE_WEIGHTS.get(rel_type, 1.0)
