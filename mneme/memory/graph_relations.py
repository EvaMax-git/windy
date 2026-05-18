"""P8-01 Graph Relations — automatic edge creation between memories.

When a memory is created or activated, this module analyzes it against
existing memories and auto-creates edges (``memory_relations`` rows) based
on embedding similarity, temporal proximity, and content heuristics.

Edge types
----------
+-------------+---------------------------+----------------------------------+
| Edge Type   | Trigger                   | Threshold / Logic               |
+=============+===========================+==================================+
| similar     | cosine >= 0.92            | near-duplicate                  |
+-------------+---------------------------+----------------------------------+
| references  | 0.80 <= cosine < 0.92     | moderate-high similarity         |
+-------------+---------------------------+----------------------------------+
| contradicts | 0.70 <= cosine < 0.85     | conflict-zone (semantic tens.)   |
+-------------+---------------------------+----------------------------------+
| temporal    | activated_at within       | sequential events near in time   |
|             | 30 min of each other      |                                  |
+-------------+---------------------------+----------------------------------+
| causal      | node_type reflection→fact | heuristic: reflection may have   |
|             | or fact→fact in sequence  | caused the subsequent fact       |
+-------------+---------------------------+----------------------------------+

All edges are created as ``active`` and can be reviewed/resolved via the
existing ``memory_relations`` API.

Usage
-----
Called automatically from ``activate_memory`` and ``create_memory``:

.. code-block:: python

    from mneme.memory.graph_relations import auto_create_relations

    auto_create_relations(db, context, memory_id=new_memory_id)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session

from mneme.api.context import RequestContext
from mneme.schemas.memory_relations import MemoryRelationCreate, RelationType

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# Similarity thresholds
# ═══════════════════════════════════════════════════════════════════════════

SIMILAR_THRESHOLD = 0.92       # cosine >= this → similar edge
REFERENCE_LOW = 0.80           # cosine in [REF_LOW, SIMILAR) → references
CONTRADICT_LOW = 0.70          # cosine in [CONTRA_LOW, REF_LOW) → contradicts
TEMPORAL_WINDOW_MINUTES = 30   # activated_at within this → temporal edge
MAX_AUTO_EDGES = 15            # max edges created per invocation


# ═══════════════════════════════════════════════════════════════════════════
# Dataclasses
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class GraphEdgeCandidate:
    """A candidate pair (source → target memory) for auto-edge creation."""

    source_memory_id: UUID
    target_memory_id: UUID
    target_title: str | None = None
    target_canonical_key: str | None = None
    target_node_type: str | None = None
    target_status: str | None = None
    source_activated_at: datetime | None = None
    target_activated_at: datetime | None = None
    cosine_similarity: float = 0.0


@dataclass
class AutoEdgeResult:
    """Aggregated result from auto-edge creation."""

    memory_id: UUID
    candidates_considered: int = 0
    similar_created: int = 0
    references_created: int = 0
    contradicts_created: int = 0
    temporal_created: int = 0
    causal_created: int = 0
    total_edges: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def edges_summary(self) -> dict[str, int]:
        return {
            "similar": self.similar_created,
            "references": self.references_created,
            "contradicts": self.contradicts_created,
            "temporal": self.temporal_created,
            "causal": self.causal_created,
            "total": self.total_edges,
        }


# ═══════════════════════════════════════════════════════════════════════════
# Internal — embedding helpers (mirrors refine/conflict.py pattern)
# ═══════════════════════════════════════════════════════════════════════════

import json


def _parse_stored_embedding(value) -> list[float] | None:
    """Parse a stored embedding value from DB (list, string-JSON, or pgvector)."""
    if value is None:
        return None
    if isinstance(value, list):
        raw = value
    elif isinstance(value, str):
        try:
            raw = json.loads(value)
        except json.JSONDecodeError:
            return None
    else:
        return None
    try:
        return [float(v) for v in raw]
    except (TypeError, ValueError):
        return None


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two embedding vectors."""
    if len(a) != len(b) or len(a) == 0:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


# ═══════════════════════════════════════════════════════════════════════════
# Internal — fetch neighbor candidates
# ═══════════════════════════════════════════════════════════════════════════

_FETCH_NEIGHBOR_CANDIDATES = text("""
    SELECT DISTINCT ON (mie.memory_id)
        mie.memory_index_entry_id,
        mie.memory_id,
        mie.embedding,
        mie.memory_version,
        m.title,
        m.memory_text,
        m.canonical_key,
        m.node_type,
        m.status,
        m.activated_at,
        m.project_id
    FROM memory_index_entries mie
    JOIN memories m ON m.memory_id = mie.memory_id
    WHERE mie.vector_state = 'ready'
      AND mie.embedding IS NOT NULL
      AND m.status = 'active'
      AND m.memory_id != :source_mid
      AND (:project_id IS NULL OR mie.project_id = :project_id)
    ORDER BY mie.memory_id, mie.memory_version DESC
""").bindparams(
    bindparam("source_mid", type_=PG_UUID(as_uuid=True)),
    bindparam("project_id", type_=PG_UUID(as_uuid=True)),
)


def _fetch_neighbors(
    db: Session,
    *,
    source_memory_id: UUID,
    project_id: UUID | None = None,
) -> list[dict[str, Any]]:
    """Return active memories (with embeddings) in the same project."""
    rows = db.execute(
        _FETCH_NEIGHBOR_CANDIDATES,
        {"source_mid": source_memory_id, "project_id": project_id},
    ).all()

    results: list[dict[str, Any]] = []
    for row in rows:
        data = dict(row._mapping)
        embedding = _parse_stored_embedding(data.get("embedding"))
        if embedding is None:
            continue
        data["embedding_vector"] = embedding
        results.append(data)
    return results


# ═══════════════════════════════════════════════════════════════════════════
# Internal — fetch source memory embedding
# ═══════════════════════════════════════════════════════════════════════════

_FETCH_SOURCE_EMBEDDING = text("""
    SELECT mie.embedding, m.activated_at, m.node_type, m.project_id
    FROM memory_index_entries mie
    JOIN memories m ON m.memory_id = mie.memory_id
    WHERE mie.memory_id = :mid
      AND mie.vector_state = 'ready'
      AND mie.embedding IS NOT NULL
    ORDER BY mie.memory_version DESC
    LIMIT 1
""").bindparams(bindparam("mid", type_=PG_UUID(as_uuid=True)))


# ═══════════════════════════════════════════════════════════════════════════
# Internal — create a single edge (idempotent via ON CONFLICT)
# ═══════════════════════════════════════════════════════════════════════════

_INSERT_EDGE_IDEMPOTENT = text("""
    INSERT INTO memory_relations (
      memory_relation_id, project_id,
      from_memory_id, from_memory_version,
      to_memory_id, to_memory_version,
      relation_type, relation_status,
      reason, metadata_json
    ) VALUES (
      :rid, :pid,
      :from_mid, :from_ver,
      :to_mid, :to_ver,
      :rtype, 'active',
      :reason, :meta
    )
    ON CONFLICT (from_memory_id, to_memory_id, relation_type) DO NOTHING
    RETURNING memory_relation_id
""").bindparams(
    bindparam("rid", type_=PG_UUID(as_uuid=True)),
    bindparam("pid", type_=PG_UUID(as_uuid=True)),
    bindparam("from_mid", type_=PG_UUID(as_uuid=True)),
    bindparam("to_mid", type_=PG_UUID(as_uuid=True)),
)


def _try_create_edge(
    db: Session,
    *,
    from_memory_id: UUID,
    from_version: int,
    to_memory_id: UUID,
    to_version: int,
    relation_type: str,
    reason: str,
    project_id: UUID | None,
    metadata: dict[str, Any] | None = None,
) -> bool:
    """Create an edge, returning True if inserted (False if duplicate)."""
    rid = uuid4()
    meta_json = json.dumps(metadata or {})
    result = db.execute(
        _INSERT_EDGE_IDEMPOTENT,
        {
            "rid": rid,
            "pid": project_id,
            "from_mid": from_memory_id,
            "from_ver": from_version,
            "to_mid": to_memory_id,
            "to_ver": to_version,
            "rtype": relation_type,
            "reason": reason,
            "meta": meta_json,
        },
    ).first()
    return result is not None


# ═══════════════════════════════════════════════════════════════════════════
# Internal — get memory metadata (version, project_id)
# ═══════════════════════════════════════════════════════════════════════════

_GET_MEM_META = text("""
    SELECT current_version, project_id, node_type, activated_at, title, canonical_key
    FROM memories WHERE memory_id = :mid
""").bindparams(bindparam("mid", type_=PG_UUID(as_uuid=True)))


def _get_memory_meta(db: Session, memory_id: UUID) -> dict[str, Any] | None:
    """Return version, project_id, node_type, activated_at for a memory."""
    row = db.execute(_GET_MEM_META, {"mid": memory_id}).first()
    if row is None:
        return None
    return dict(row._mapping)


# ═══════════════════════════════════════════════════════════════════════════
# Public API — auto_create_relations
# ═══════════════════════════════════════════════════════════════════════════


def auto_create_relations(
    db: Session,
    context: RequestContext,
    *,
    memory_id: UUID,
    max_edges: int = MAX_AUTO_EDGES,
) -> AutoEdgeResult:
    """Analyze *memory_id* against existing active memories and auto-create graph edges.

    This is called after a memory is created or activated.  It:
    1. Fetches the source memory's embedding (if ready).
    2. Fetches all active neighbors with embeddings.
    3. Computes cosine similarity and classifies into edge types.
    4. Creates *similar*, *references*, *contradicts*, *temporal*, *causal* edges
       via idempotent INSERT.

    Parameters
    ----------
    db : Session
        Active SQLAlchemy session.
    context : RequestContext
        Tracing / auth context (not used for edges currently — edges are
        idempotent system-created records).
    memory_id : UUID
        The newly created/activated memory to analyze.
    max_edges : int
        Maximum edges to create per invocation (default 15).

    Returns
    -------
    AutoEdgeResult
        Summary of what was created.
    """
    result = AutoEdgeResult(memory_id=memory_id)

    # 1. Get source memory metadata
    src_meta = _get_memory_meta(db, memory_id)
    if src_meta is None:
        result.errors.append(f"memory {memory_id} not found")
        return result

    src_version = src_meta["current_version"]
    src_project_id = src_meta["project_id"]
    src_node_type = src_meta["node_type"]
    src_activated_at = src_meta["activated_at"]

    # 2. Get source embedding
    src_row = db.execute(_FETCH_SOURCE_EMBEDDING, {"mid": memory_id}).first()
    src_embedding: list[float] | None = None
    if src_row:
        emb = _parse_stored_embedding(dict(src_row._mapping).get("embedding"))
        if emb:
            src_embedding = emb

    # 3. Fetch neighbor candidates
    neighbors = _fetch_neighbors(db, source_memory_id=memory_id, project_id=src_project_id)
    result.candidates_considered = len(neighbors)
    logger.info(
        "graph_relations: source memory %s, %d neighbor candidates (embedding=%s)",
        memory_id, len(neighbors), src_embedding is not None,
    )

    # 4. Score and classify candidates
    candidates: list[GraphEdgeCandidate] = []
    for nb in neighbors:
        target_id = nb["memory_id"]
        sim = 0.0
        if src_embedding is not None and "embedding_vector" in nb:
            sim = _cosine_similarity(src_embedding, nb["embedding_vector"])
        candidates.append(
            GraphEdgeCandidate(
                source_memory_id=memory_id,
                target_memory_id=target_id,
                target_title=nb.get("title"),
                target_canonical_key=nb.get("canonical_key"),
                target_node_type=nb.get("node_type"),
                target_status=nb.get("status", "active"),
                source_activated_at=src_activated_at,
                target_activated_at=nb.get("activated_at"),
                cosine_similarity=round(sim, 6),
            )
        )

    # Sort by similarity descending (best matches first)
    candidates.sort(key=lambda c: c.cosine_similarity, reverse=True)

    # 5. Create edges (respecting max_edges cap)
    edges_created = 0
    for candidate in candidates:
        if edges_created >= max_edges:
            break

        edge_created = _classify_and_create_edge(
            db, candidate, src_version, src_project_id, src_node_type,
        )
        if edge_created:
            edges_created += 1
            result = _count_edge_type(result, candidate)

    result.total_edges = edges_created
    logger.info(
        "graph_relations: %d edges created for memory %s (%s)",
        edges_created, memory_id, result.edges_summary,
    )
    return result


def _classify_and_create_edge(
    db: Session,
    candidate: GraphEdgeCandidate,
    src_version: int,
    project_id: UUID | None,
    src_node_type: str | None = None,
) -> bool:
    """Classify a candidate pair and create the appropriate edge type.

    Priority order (first match wins):
    1. similar (sim >= 0.92)
    2. references (0.80 <= sim < 0.92)
    3. contradicts (0.70 <= sim < 0.85)
    4. temporal (time proximity)
    5. causal (heuristic based on node_types)
    """
    sim = candidate.cosine_similarity

    # Get target version
    tgt_meta = _get_memory_meta(db, candidate.target_memory_id)
    tgt_version = tgt_meta["current_version"] if tgt_meta else 1

    # ── similar (near-duplicate) ──
    if sim >= SIMILAR_THRESHOLD:
        return _try_create_edge(
            db,
            from_memory_id=candidate.source_memory_id,
            from_version=src_version,
            to_memory_id=candidate.target_memory_id,
            to_version=tgt_version,
            relation_type="similar",
            reason=f"auto: cosine={sim:.4f} (>= {SIMILAR_THRESHOLD})",
            project_id=project_id,
            metadata={
                "cosine_similarity": sim,
                "source": "graph_relations",
                "method": "embedding",
            },
        )

    # ── references (moderate-high similarity) ──
    if REFERENCE_LOW <= sim < SIMILAR_THRESHOLD:
        return _try_create_edge(
            db,
            from_memory_id=candidate.source_memory_id,
            from_version=src_version,
            to_memory_id=candidate.target_memory_id,
            to_version=tgt_version,
            relation_type="references",
            reason=f"auto: cosine={sim:.4f} (>= {REFERENCE_LOW}, < {SIMILAR_THRESHOLD})",
            project_id=project_id,
            metadata={
                "cosine_similarity": sim,
                "source": "graph_relations",
                "method": "embedding",
            },
        )

    # ── contradicts (conflict zone) ──
    if CONTRADICT_LOW <= sim < REFERENCE_LOW:
        return _try_create_edge(
            db,
            from_memory_id=candidate.source_memory_id,
            from_version=src_version,
            to_memory_id=candidate.target_memory_id,
            to_version=tgt_version,
            relation_type="contradicts",
            reason=f"auto: cosine={sim:.4f} (conflict zone [{CONTRADICT_LOW}, {REFERENCE_LOW}))",
            project_id=project_id,
            metadata={
                "cosine_similarity": sim,
                "source": "graph_relations",
                "method": "embedding",
                "needs_review": True,
            },
        )

    # ── temporal (time-based proximity) ──
    if _is_temporal_neighbor(candidate):
        return _try_create_edge(
            db,
            from_memory_id=candidate.source_memory_id,
            from_version=src_version,
            to_memory_id=candidate.target_memory_id,
            to_version=tgt_version,
            relation_type="temporal",
            reason="auto: activated within temporal window",
            project_id=project_id,
            metadata={
                "source_activated_at": (
                    candidate.source_activated_at.isoformat()
                    if candidate.source_activated_at else None
                ),
                "target_activated_at": (
                    candidate.target_activated_at.isoformat()
                    if candidate.target_activated_at else None
                ),
                "source": "graph_relations",
                "method": "temporal",
            },
        )

    # ── causal (heuristic: node_type patterns) ──
    if _is_causal_candidate(candidate, src_node_type):
        return _try_create_edge(
            db,
            from_memory_id=candidate.source_memory_id,
            from_version=src_version,
            to_memory_id=candidate.target_memory_id,
            to_version=tgt_version,
            relation_type="causal",
            reason=_causal_reason(candidate, src_node_type),
            project_id=project_id,
            metadata={
                "source_node_type": src_node_type,
                "target_node_type": candidate.target_node_type,
                "source": "graph_relations",
                "method": "heuristic",
                "needs_review": True,
            },
        )

    return False


# ═══════════════════════════════════════════════════════════════════════════
# Edge-classifier helpers
# ═══════════════════════════════════════════════════════════════════════════


def _is_temporal_neighbor(candidate: GraphEdgeCandidate) -> bool:
    """Check if two memories are temporally close."""
    src_at = candidate.source_activated_at
    tgt_at = candidate.target_activated_at
    if src_at is None or tgt_at is None:
        return False
    # Ensure timezone-aware
    if src_at.tzinfo is None:
        src_at = src_at.replace(tzinfo=timezone.utc)
    if tgt_at.tzinfo is None:
        tgt_at = tgt_at.replace(tzinfo=timezone.utc)
    delta = abs((src_at - tgt_at).total_seconds()) / 60.0
    return delta <= TEMPORAL_WINDOW_MINUTES


def _is_causal_candidate(
    candidate: GraphEdgeCandidate,
    src_node_type: str | None = None,
) -> bool:
    """Heuristic: memories with certain node_type combos may have causal links.

    - reflection → fact: reflection may have caused/derived the fact
    - fact → fact: one fact might lead to another
    - episode → fact: episode may be evidence for a fact
    """
    if src_node_type is None or candidate.target_node_type is None:
        return False

    causal_pairs = {
        ("reflection", "fact"),
        ("fact", "fact"),
        ("episode", "fact"),
        ("reflection", "concept"),
        ("episode", "reflection"),
    }
    return (src_node_type, candidate.target_node_type) in causal_pairs


def _causal_reason(
    candidate: GraphEdgeCandidate,
    src_node_type: str | None = None,
) -> str:
    """Generate a human-readable reason for a causal edge."""
    src_nt = src_node_type or "unknown"
    tgt_nt = candidate.target_node_type or "unknown"
    return (
        f"auto: heuristic causal link ({src_nt} → {tgt_nt})"
        f" | src={candidate.source_memory_id} tgt={candidate.target_memory_id}"
    )


def _count_edge_type(result: AutoEdgeResult, candidate: GraphEdgeCandidate) -> AutoEdgeResult:
    """Increment the appropriate counter based on the highest-priority match."""
    sim = candidate.cosine_similarity
    if sim >= SIMILAR_THRESHOLD:
        result.similar_created += 1
    elif sim >= REFERENCE_LOW:
        result.references_created += 1
    elif sim >= CONTRADICT_LOW:
        result.contradicts_created += 1
    elif _is_temporal_neighbor(candidate):
        result.temporal_created += 1
    elif _is_causal_candidate(candidate, src_node_type=None):  # re-check
        result.causal_created += 1
    return result


# ═══════════════════════════════════════════════════════════════════════════
# Public API — text-based fallback (no embedding required)
# ═══════════════════════════════════════════════════════════════════════════

_JACCARD_SIMILAR_NEIGHBORS = text("""
    SELECT
        m.memory_id, m.title, m.canonical_key, m.node_type,
        m.status, m.activated_at, m.current_version,
        m.project_id
    FROM memories m
    WHERE m.memory_id != :source_mid
      AND m.status = 'active'
      AND (:project_id IS NULL OR m.project_id = :project_id)
      AND (
          m.memory_text ILIKE '%' || :keyword1 || '%'
          OR m.title ILIKE '%' || :keyword1 || '%'
        )
    LIMIT :limit
""").bindparams(
    bindparam("source_mid", type_=PG_UUID(as_uuid=True)),
    bindparam("project_id", type_=PG_UUID(as_uuid=True)),
)


def _extract_keywords(text: str, max_words: int = 5) -> list[str]:
    """Extract meaningful keywords from text (simple fallback)."""
    # Remove short/common words, take first longest words
    words = text.lower().split()
    # Filter words >= 4 chars, non-stopword heuristics
    stopwords = {
        "this", "that", "with", "from", "have", "been", "were", "they",
        "will", "would", "could", "should", "about", "there", "their",
        "which", "when", "what", "where", "also", "than", "then", "just",
        "some", "only", "over", "into", "such", "more", "very", "much",
        "other", "after", "still", "being", "been",
    }
    candidates = [w for w in words if len(w) >= 4 and w not in stopwords]
    # Return longest unique words
    unique = list(dict.fromkeys(candidates))
    return sorted(unique, key=len, reverse=True)[:max_words]


def auto_create_relations_text(
    db: Session,
    context: RequestContext,
    *,
    memory_id: UUID,
    max_edges: int = MAX_AUTO_EDGES,
) -> AutoEdgeResult:
    """Text-based fallback when embeddings are not available.

    Uses keyword overlap (Jaccard-like) instead of cosine similarity.
    Only creates ``references`` edges.
    """
    from mneme.db.memories import get_memory

    result = AutoEdgeResult(memory_id=memory_id)

    src_mem = get_memory(db, memory_id)
    if src_mem is None:
        result.errors.append(f"memory {memory_id} not found")
        return result

    src_meta = _get_memory_meta(db, memory_id)
    if src_meta is None:
        result.errors.append(f"memory {memory_id} meta not found")
        return result

    src_version = src_meta["current_version"]
    src_project_id = src_meta["project_id"]
    src_node_type = src_meta["node_type"]
    activation_dt = src_meta.get("activated_at")
    if isinstance(activation_dt, str):
        try:
            activation_dt = datetime.fromisoformat(activation_dt)
        except ValueError:
            activation_dt = None

    keywords = _extract_keywords(src_mem.memory_text)
    if not keywords:
        return result

    # Query neighbors by keyword overlap
    edges_created = 0
    for kw in keywords[:3]:  # top 3 keywords
        if edges_created >= max_edges:
            break
        rows = db.execute(
            _JACCARD_SIMILAR_NEIGHBORS,
            {
                "source_mid": memory_id,
                "project_id": src_project_id,
                "keyword1": kw,
                "limit": max_edges - edges_created,
            },
        ).all()

        for row in rows:
            if edges_created >= max_edges:
                break
            data = dict(row._mapping)
            tgt_id = data["memory_id"]

            # Compute rough Jaccard
            tgt_mem = get_memory(db, tgt_id)
            if tgt_mem is None:
                continue
            src_words = set(src_mem.memory_text.lower().split())
            tgt_words = set(tgt_mem.memory_text.lower().split())
            if not src_words or not tgt_words:
                continue
            intersection = src_words & tgt_words
            union = src_words | tgt_words
            jaccard = len(intersection) / len(union) if union else 0.0

            # Only create references edge for Jaccard >= 0.15
            if jaccard < 0.15:
                continue

            tgt_version = data.get("current_version", 1)
            created = _try_create_edge(
                db,
                from_memory_id=memory_id,
                from_version=src_version,
                to_memory_id=tgt_id,
                to_version=tgt_version,
                relation_type="references",
                reason=f"auto: text keyword overlap (jaccard={jaccard:.4f})",
                project_id=src_project_id,
                metadata={
                    "jaccard_similarity": round(jaccard, 4),
                    "keyword": kw,
                    "source": "graph_relations",
                    "method": "text",
                },
            )
            if created:
                edges_created += 1
                result.references_created += 1

    result.candidates_considered = edges_created
    result.total_edges = edges_created
    return result


# ═══════════════════════════════════════════════════════════════════════════
# Convenience — auto-detect method and create edges
# ═══════════════════════════════════════════════════════════════════════════


def auto_create_relations_smart(
    db: Session,
    context: RequestContext,
    *,
    memory_id: UUID,
    max_edges: int = MAX_AUTO_EDGES,
) -> AutoEdgeResult:
    """Try embedding-based auto-relation first; fall back to text-based.

    Parameters
    ----------
    db : Session
        Active SQLAlchemy session.
    context : RequestContext
        Auth / tracing context.
    memory_id : UUID
        The newly created/activated memory.
    max_edges : int
        Maximum edges per invocation.

    Returns
    -------
    AutoEdgeResult
    """
    # Check if embedding exists
    src_row = db.execute(_FETCH_SOURCE_EMBEDDING, {"mid": memory_id}).first()
    has_embedding = False
    if src_row:
        emb = _parse_stored_embedding(dict(src_row._mapping).get("embedding"))
        has_embedding = emb is not None

    if has_embedding:
        logger.info("graph_relations: using embedding method for memory %s", memory_id)
        return auto_create_relations(db, context, memory_id=memory_id, max_edges=max_edges)
    else:
        logger.info(
            "graph_relations: embedding not ready, using text fallback for memory %s",
            memory_id,
        )
        return auto_create_relations_text(
            db, context, memory_id=memory_id, max_edges=max_edges,
        )
