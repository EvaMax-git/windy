"""P6-02.1 Memory Refine — duplicate detection via embedding cosine similarity.

This module detects near-duplicate memory pairs by computing pairwise cosine
similarity of their stored embedding vectors.  High-similarity pairs (≥ 0.92)
are flagged as ``duplicates`` candidates.

Algorithm
---------
1. Query all ``status='active'`` + ``vector_state='ready'`` memories with
   their embeddings (one entry per memory, latest version) via inline SQL.
2. Compute pairwise cosine similarity inside each project.
3. Pairs whose similarity ≥ ``threshold`` are returned as ``DedupPair``.
4. ``dry_run=True`` → return candidates only, no DB writes.
5. ``dry_run=False`` → create ``memory_relations(type='duplicates')`` +
   generate a ``review_item`` for human confirmation.

Dependencies
------------
* ``_cosine_similarity`` from ``mneme.memory.search`` — existing.
* ``create_memory_relation`` from ``mneme.db.memory_relations`` — existing.
* ``create_review_item`` from ``mneme.db.review_items`` — existing.
* Inline SQL for embedding fetch — no external stub required.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session

from mneme.api.context import RequestContext
from mneme.memory.search import _cosine_similarity

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# Dataclasses
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class DedupPair:
    """A candidate duplicate pair detected by embedding cosine similarity."""

    memory_a_id: UUID
    memory_b_id: UUID
    similarity: float
    memory_a_title: str | None = None
    memory_b_title: str | None = None
    canonical_key_a: str | None = None
    canonical_key_b: str | None = None

    @property
    def is_identical(self) -> bool:
        """True when similarity is near-perfect (≥ 0.9999)."""
        return self.similarity >= 0.9999


@dataclass
class DedupResult:
    """Aggregated result from a dedup scan."""

    pairs_found: int = 0
    relations_created: int = 0
    pairs: list[DedupPair] = field(default_factory=list)

    @property
    def identical_count(self) -> int:
        """Number of near-identical pairs (similarity ≥ 0.9999)."""
        return sum(1 for p in self.pairs if p.is_identical)


# ═══════════════════════════════════════════════════════════════════════════
# Internal — embedding fetch (inline SQL, no external stub dependent)
# ═══════════════════════════════════════════════════════════════════════════

_READY_EMBEDDING_CANDIDATES = text("""
    SELECT DISTINCT ON (mie.memory_id)
        mie.memory_index_entry_id,
        mie.memory_id,
        mie.embedding,
        mie.memory_version,
        m.title,
        m.memory_text,
        m.canonical_key,
        m.project_id,
        m.created_at
    FROM memory_index_entries mie
    JOIN memories m ON m.memory_id = mie.memory_id
    WHERE mie.vector_state = 'ready'
      AND mie.embedding IS NOT NULL
      AND m.status = 'active'
      AND (:project_id IS NULL OR mie.project_id = :project_id)
    ORDER BY mie.memory_id, mie.memory_version DESC
""").bindparams(bindparam("project_id", type_=PG_UUID(as_uuid=True)))


def _parse_stored_embedding(value) -> list[float] | None:
    """Parse an embedding value (may be list, JSON string, or raw)."""
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


def _fetch_active_with_embeddings(
    db: Session,
    *,
    project_id: UUID | None = None,
) -> list[dict]:
    """Return dicts for active memories with ready embeddings.

    Each dict: memory_id, embedding (list[float]), title, memory_text,
    canonical_key, project_id.
    """
    rows = db.execute(
        _READY_EMBEDDING_CANDIDATES,
        {"project_id": project_id},
    ).all()

    results: list[dict] = []
    for row in rows:
        data = dict(row._mapping)
        embedding = _parse_stored_embedding(data.get("embedding"))
        if embedding is None:
            continue
        results.append(
            {
                "memory_id": data["memory_id"],
                "embedding": embedding,
                "title": data.get("title"),
                "memory_text": data.get("memory_text"),
                "canonical_key": data.get("canonical_key", ""),
                "project_id": data.get("project_id"),
            }
        )
    return results


# ═══════════════════════════════════════════════════════════════════════════
# Internal — pairwise similarity
# ═══════════════════════════════════════════════════════════════════════════

def _pairwise_similar(
    entries: list[dict],
    *,
    threshold: float = 0.92,
    max_candidates: int = 50,
) -> list[DedupPair]:
    """Compute pairwise cosine similarity and return pairs >= threshold."""
    pairs: list[DedupPair] = []
    n = len(entries)
    if n < 2:
        return pairs

    for i in range(n):
        for j in range(i + 1, n):
            a, b = entries[i], entries[j]
            sim = _cosine_similarity(a["embedding"], b["embedding"])
            if sim >= threshold:
                pairs.append(
                    DedupPair(
                        memory_a_id=a["memory_id"],
                        memory_b_id=b["memory_id"],
                        similarity=round(sim, 6),
                        memory_a_title=a.get("title"),
                        memory_b_title=b.get("title"),
                        canonical_key_a=a.get("canonical_key"),
                        canonical_key_b=b.get("canonical_key"),
                    )
                )

    pairs.sort(key=lambda p: p.similarity, reverse=True)
    return pairs[:max_candidates]


# ═══════════════════════════════════════════════════════════════════════════
# Public API — detect
# ═══════════════════════════════════════════════════════════════════════════

def detect_duplicates(
    db: Session,
    *,
    project_id: UUID | None = None,
    threshold: float = 0.92,
    max_candidates: int = 50,
) -> DedupResult:
    """Scan active memories for duplicate pairs via embedding similarity.

    Parameters
    ----------
    db : Session
        Active SQLAlchemy session.
    project_id : UUID | None
        Scope to a specific project; ``None`` scans all projects.
    threshold : float
        Cosine similarity threshold (0.5-1.0).  Pairs >= this value are
        returned.  Default ``0.92``.
    max_candidates : int
        Maximum number of candidate pairs to return (1-500).

    Returns
    -------
    DedupResult
        Aggregated result with the list of detected pairs.
    """
    if not (0.5 <= threshold <= 1.0):
        raise ValueError(f"threshold must be 0.5-1.0, got {threshold}")
    if not (1 <= max_candidates <= 500):
        raise ValueError(f"max_candidates must be 1-500, got {max_candidates}")

    entries = _fetch_active_with_embeddings(db, project_id=project_id)
    logger.info(
        "dedup: fetched %d active memories with ready embeddings (project_id=%s)",
        len(entries),
        project_id,
    )

    if len(entries) < 2:
        return DedupResult(pairs_found=0)

    pairs = _pairwise_similar(
        entries, threshold=threshold, max_candidates=max_candidates,
    )

    logger.info(
        "dedup: found %d duplicate pairs (threshold=%.3f, max=%d)",
        len(pairs),
        threshold,
        max_candidates,
    )

    return DedupResult(pairs_found=len(pairs), pairs=pairs)


# ═══════════════════════════════════════════════════════════════════════════
# Public API — apply (single pair)
# ═══════════════════════════════════════════════════════════════════════════

def apply_dedup(
    db: Session,
    context: RequestContext,
    *,
    pair: DedupPair,
    create_review: bool = True,
    reason: str = "Auto-detected by embedding cosine similarity",
) -> dict | None:
    """Persist a detected duplicate pair as a ``duplicates`` relation.

    Creates a ``memory_relations`` row (type ``duplicates``) and optionally a
    ``review_item`` for human confirmation.

    Parameters
    ----------
    db : Session
        Active SQLAlchemy session.
    context : RequestContext
        Tracing / auth context for audit + outbox.
    pair : DedupPair
        The candidate pair to persist.
    create_review : bool
        If ``True``, also creates a ``review_items`` row for human review.
    reason : str
        Human-readable reason recorded on the relation.

    Returns
    -------
    dict | None
        ``{"memory_relation": MemoryRelationRead, "review_item": dict | None}``,
        or ``None`` on error (e.g. relation already exists, memory not found).
    """
    from mneme.db.memories import get_memory
    from mneme.db.memory_relations import create_memory_relation
    from mneme.db.review_items import create_review_item
    from mneme.schemas.memory_relations import MemoryRelationCreate, RelationType

    # Guard: no self-referencing
    if pair.memory_a_id == pair.memory_b_id:
        logger.warning("dedup: skipping self-referencing pair %s", pair.memory_a_id)
        return None

    # Validate both memories exist
    mem_a = get_memory(db, pair.memory_a_id)
    mem_b = get_memory(db, pair.memory_b_id)
    if mem_a is None or mem_b is None:
        logger.warning(
            "dedup: one or both memories not found (a=%s, b=%s)",
            pair.memory_a_id,
            pair.memory_b_id,
        )
        return None

    # Create duplicates relation
    try:
        relation = create_memory_relation(
            db,
            context,
            payload=MemoryRelationCreate(
                from_memory_id=pair.memory_a_id,
                to_memory_id=pair.memory_b_id,
                relation_type=RelationType.duplicates,
                reason=reason,
                metadata_json={
                    "similarity": pair.similarity,
                    "source": "embedding_cosine_dedup",
                    "memory_a_title": pair.memory_a_title or "",
                    "memory_b_title": pair.memory_b_title or "",
                },
            ),
        )
        logger.info(
            "dedup: created duplicates relation %s (sim=%.4f)",
            relation.memory_relation_id,
            pair.similarity,
        )
    except ValueError as exc:
        # Likely UNIQUE violation or FK constraint - relation already exists
        logger.info("dedup: relation already exists or insert failed: %s", exc)
        return None

    # Optionally create review_item
    review_item = None
    if create_review:
        try:
            review_item = create_review_item(
                project_id=mem_a.project_id,
                review_type="duplicate_resolution",
                target_type="memory_relation",
                target_id=relation.memory_relation_id,
                priority=80,
                requester_actor_type=context.actor.actor_type,
                requester_actor_id=context.actor.actor_id,
                decision_payload={
                    "similarity": pair.similarity,
                    "memory_a_id": str(pair.memory_a_id),
                    "memory_b_id": str(pair.memory_b_id),
                    "memory_a_title": pair.memory_a_title or "",
                    "memory_b_title": pair.memory_b_title or "",
                    "canonical_key_a": pair.canonical_key_a or "",
                    "canonical_key_b": pair.canonical_key_b or "",
                },
                correlation_id=context.correlation_id,
                request_id=context.request_id,
                idempotency_key=(
                    context.idempotency_key
                    or f"dedup-review-{relation.memory_relation_id}"
                ),
            )
            logger.info(
                "dedup: created review_item %s for relation %s",
                review_item.get("review_item_id"),
                relation.memory_relation_id,
            )
        except Exception as exc:
            # Review item creation is best-effort - relation is already saved
            logger.warning("dedup: review_item creation failed (non-fatal): %s", exc)

    return {
        "memory_relation": relation,
        "review_item": review_item,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Public API — batch apply
# ═══════════════════════════════════════════════════════════════════════════

def apply_dedup_batch(
    db: Session,
    context: RequestContext,
    *,
    pairs: list[DedupPair],
    create_review: bool = True,
    reason: str = "Auto-detected by embedding cosine similarity",
) -> DedupResult:
    """Apply dedup relations for a batch of pairs.

    Each pair is applied independently; a single failure does not stop the
    batch.  Returns an aggregated ``DedupResult``.

    Parameters
    ----------
    db : Session
        Active SQLAlchemy session.
    context : RequestContext
        Tracing / auth context.
    pairs : list[DedupPair]
        Candidate pairs to persist.
    create_review : bool
        Whether to create review items for human confirmation.
    reason : str
        Human-readable reason.

    Returns
    -------
    DedupResult
        Aggregated summary of what was created.
    """
    result = DedupResult(pairs_found=len(pairs))
    for pair in pairs:
        outcome = apply_dedup(
            db,
            context,
            pair=pair,
            create_review=create_review,
            reason=reason,
        )
        if outcome is not None:
            result.relations_created += 1
            result.pairs.append(pair)

    logger.info(
        "dedup batch: %d pairs -> %d relations created",
        result.pairs_found,
        result.relations_created,
    )
    return result
