"""P6-02.2 Memory Refine — conflict detection via LLM semantic analysis.

This module detects semantically contradictory memory pairs using a two-stage
pipeline: (1) embedding similarity filtering to find "conflict zone" candidates
(0.70 <= sim < 0.92), then (2) LLM evaluation to determine whether the pair
contains actual contradictory facts, decisions, or constraints.

Algorithm
---------
1. Query all ``status='active'`` + ``vector_state='ready'`` memories with
   their embeddings (one entry per memory, latest version).
2. Compute pairwise cosine similarity; keep pairs where
   ``threshold_low <= sim < threshold_high`` (default 0.70-0.92).
3. Send each candidate pair to an LLM with the conflict detection prompt.
4. LLM returns ``{"conflict": bool, "reason": str, "confidence": float}``.
5. ``conflict=True`` AND ``confidence >= 0.7`` → confirmed conflict.
6. Create ``memory_relations(type='conflicts_with')`` + ``review_item``.

Dependencies
------------
* ``_cosine_similarity`` from ``mneme.memory.search`` — existing.
* ``Gateway`` from ``mneme.gateway.call`` — existing.
* ``create_memory_relation`` from ``mneme.db.memory_relations`` — existing.
* ``create_review_item`` from ``mneme.db.review_items`` — existing.
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
# LLM Prompt
# ═══════════════════════════════════════════════════════════════════════════

_CONFLICT_SYSTEM = (
    "You are a memory conflict detector. Given two memory statements, "
    "determine if they contain contradictory facts, decisions, or constraints.\n\n"
    "Respond with JSON only:\n"
    '{"conflict": true/false, "reason": "<explanation>", "confidence": 0.0-1.0}'
)


# ═══════════════════════════════════════════════════════════════════════════
# Dataclasses
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class ConflictCandidate:
    """A memory pair in the similarity conflict-zone, awaiting LLM evaluation."""

    memory_a_id: UUID
    memory_b_id: UUID
    similarity: float
    memory_a_title: str | None = None
    memory_b_title: str | None = None
    memory_a_text: str | None = None
    memory_b_text: str | None = None
    canonical_key_a: str | None = None
    canonical_key_b: str | None = None

    # Populated after LLM evaluation
    conflict: bool = False
    reason: str | None = None
    confidence: float = 0.0


@dataclass
class ConflictResult:
    """Aggregated output from a conflict detection run."""

    candidates_found: int = 0
    """Total candidate pairs in the similarity zone."""

    llm_evaluated: int = 0
    """Number of pairs sent to LLM."""

    conflicts_confirmed: int = 0
    """Number of pairs confirmed as actual conflicts."""

    relations_created: int = 0
    """Number of ``conflicts_with`` relations written."""

    candidates: list[ConflictCandidate] = field(default_factory=list)
    """All candidate pairs with evaluation results."""

    @property
    def confirmed_conflicts(self) -> list[ConflictCandidate]:
        """Subset of candidates where LLM confirmed a conflict."""
        return [c for c in self.candidates if c.conflict]


# ═══════════════════════════════════════════════════════════════════════════
# Internal — embedding fetch (same SQL as dedup, inline, no stub)
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
    """Parse an embedding value from the DB."""
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
    """Return dicts for active memories with ready embeddings."""
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
# Internal — pairwise similarity filtering
# ═══════════════════════════════════════════════════════════════════════════

def _pairwise_similar_zone(
    entries: list[dict],
    *,
    threshold_low: float = 0.70,
    threshold_high: float = 0.92,
    max_pairs: int = 30,
) -> list[ConflictCandidate]:
    """Find pairs whose similarity falls in [threshold_low, threshold_high).

    These are "similar enough to be related, but not so similar as to be
    duplicates" — the ideal candidates for conflict detection.
    """
    candidates: list[ConflictCandidate] = []
    n = len(entries)
    if n < 2:
        return candidates

    for i in range(n):
        for j in range(i + 1, n):
            a, b = entries[i], entries[j]
            sim = _cosine_similarity(a["embedding"], b["embedding"])
            if threshold_low <= sim < threshold_high:
                candidates.append(
                    ConflictCandidate(
                        memory_a_id=a["memory_id"],
                        memory_b_id=b["memory_id"],
                        similarity=round(sim, 6),
                        memory_a_title=a.get("title"),
                        memory_b_title=b.get("title"),
                        memory_a_text=a.get("memory_text"),
                        memory_b_text=b.get("memory_text"),
                        canonical_key_a=a.get("canonical_key"),
                        canonical_key_b=b.get("canonical_key"),
                    )
                )

    candidates.sort(key=lambda c: c.similarity, reverse=True)
    return candidates[:max_pairs]


# ═══════════════════════════════════════════════════════════════════════════
# Internal — LLM response parsing
# ═══════════════════════════════════════════════════════════════════════════

def _parse_llm_conflict_response(raw_content: str) -> dict:
    """Parse LLM JSON response; returns {"conflict": bool, "reason": str, "confidence": float}.

    Handles common wrapping artifacts like ```json fences.
    """
    content = raw_content.strip()
    # Strip markdown code fences
    if content.startswith("```"):
        lines = content.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        content = "\n".join(lines).strip()

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        logger.warning("conflict: LLM returned non-JSON: %s", raw_content[:200])
        return {"conflict": False, "reason": "parse_error", "confidence": 0.0}

    return {
        "conflict": bool(parsed.get("conflict", False)),
        "reason": str(parsed.get("reason", "")),
        "confidence": float(parsed.get("confidence", 0.0)),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Public API — detect (similarity zone candidates)
# ═══════════════════════════════════════════════════════════════════════════

def detect_conflicts(
    db: Session,
    *,
    project_id: UUID | None = None,
    threshold_low: float = 0.70,
    threshold_high: float = 0.92,
    max_pairs: int = 30,
) -> ConflictResult:
    """Find memory pairs in the "conflict zone" (similar but not duplicates).

    Returns candidates sorted by similarity descending, capped at *max_pairs*.
    This does NOT call the LLM — use :func:`evaluate_conflicts_with_llm` for that.

    Parameters
    ----------
    db : Session
        Active SQLAlchemy session.
    project_id : UUID | None
        Scope to a specific project; ``None`` scans all projects.
    threshold_low : float
        Lower bound for similarity (0.0-1.0). Default ``0.70``.
    threshold_high : float
        Upper bound for similarity (0.0-1.0). Default ``0.92``.
    max_pairs : int
        Maximum candidate pairs to return (1-500).

    Returns
    -------
    ConflictResult
    """
    if not (0.0 <= threshold_low < threshold_high <= 1.0):
        raise ValueError(
            f"threshold_low ({threshold_low}) must be < threshold_high ({threshold_high})"
        )
    if not (1 <= max_pairs <= 500):
        raise ValueError(f"max_pairs must be 1-500, got {max_pairs}")

    entries = _fetch_active_with_embeddings(db, project_id=project_id)
    logger.info(
        "conflict: fetched %d active memories with ready embeddings (project_id=%s)",
        len(entries),
        project_id,
    )

    if len(entries) < 2:
        return ConflictResult(candidates_found=0)

    candidates = _pairwise_similar_zone(
        entries,
        threshold_low=threshold_low,
        threshold_high=threshold_high,
        max_pairs=max_pairs,
    )

    logger.info(
        "conflict: found %d candidates in similarity zone [%.2f, %.2f)",
        len(candidates),
        threshold_low,
        threshold_high,
    )

    return ConflictResult(
        candidates_found=len(candidates),
        candidates=candidates,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Public API — LLM evaluation
# ═══════════════════════════════════════════════════════════════════════════

def evaluate_conflicts_with_llm(
    candidates: list[ConflictCandidate],
    *,
    gateway=None,
    project_id: UUID | None = None,
    model: str = "deepseek-chat",
) -> ConflictResult:
    """Use LLM to evaluate whether candidate pairs are actual conflicts.

    Modifies *candidates* in-place by setting ``conflict``, ``reason``,
    and ``confidence`` on each.  If *gateway* is ``None``, pairs are
    returned unchanged (all ``conflict=False``).

    Parameters
    ----------
    candidates : list[ConflictCandidate]
        Candidates from :func:`detect_conflicts`.
    gateway : Gateway | None
        Pre-configured Gateway instance for LLM calls.
    project_id : UUID | None
        Project context for budget/capability binding.
    model : str
        Model name to use for conflict evaluation.

    Returns
    -------
    ConflictResult
        Aggregated result with evaluation counts.
    """
    if gateway is None or not candidates:
        return ConflictResult(
            candidates_found=len(candidates),
            llm_evaluated=0,
            candidates=candidates,
        )

    from mneme.gateway.call import GatewayError

    evaluated = 0
    for candidate in candidates:
        messages = [
            {"role": "system", "content": _CONFLICT_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"Memory A: {candidate.memory_a_text}\n\n"
                    f"Memory B: {candidate.memory_b_text}"
                ),
            },
        ]
        try:
            result = gateway.call(
                capability_code="chat.completion",
                params={
                    "model": model,
                    "messages": messages,
                    "temperature": 0.1,
                    "max_tokens": 512,
                },
                project_id=project_id,
                sensitivity="private",
                call_type="memory_conflict_detect",
            )
            content = (
                result.get("data", {})
                .get("choices", [{}])[0]
                .get("message", {})
                .get("content", "{}")
            )
            parsed = _parse_llm_conflict_response(content)
            candidate.conflict = parsed["conflict"]
            candidate.reason = parsed["reason"]
            candidate.confidence = parsed["confidence"]
            evaluated += 1
        except (GatewayError, Exception) as exc:
            logger.warning(
                "conflict: LLM evaluation failed for %s<->%s: %s",
                candidate.memory_a_id,
                candidate.memory_b_id,
                exc,
            )

    confirmed = sum(1 for c in candidates if c.conflict)
    logger.info(
        "conflict: LLM evaluated %d/%d pairs, %d confirmed conflicts",
        evaluated,
        len(candidates),
        confirmed,
    )

    return ConflictResult(
        candidates_found=len(candidates),
        llm_evaluated=evaluated,
        conflicts_confirmed=confirmed,
        candidates=candidates,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Public API — apply (single pair)
# ═══════════════════════════════════════════════════════════════════════════

def apply_conflict(
    db: Session,
    context: RequestContext,
    *,
    candidate: ConflictCandidate,
    min_confidence: float = 0.7,
    create_review: bool = True,
) -> dict | None:
    """Create a ``conflicts_with`` relation for a confirmed conflict pair.

    Only writes if ``candidate.conflict is True`` **and**
    ``candidate.confidence >= min_confidence``.

    Parameters
    ----------
    db : Session
        Active SQLAlchemy session.
    context : RequestContext
        Tracing / auth context for audit + outbox.
    candidate : ConflictCandidate
        The evaluated candidate pair.
    min_confidence : float
        Minimum LLM confidence to act (0.0-1.0). Default ``0.7``.
    create_review : bool
        If ``True``, also creates a ``review_items`` row for human review.

    Returns
    -------
    dict | None
        ``{"memory_relation": MemoryRelationRead, "review_item": dict | None}``,
        or ``None`` if skipped (not confirmed, low confidence, or error).
    """
    if not candidate.conflict or candidate.confidence < min_confidence:
        logger.debug(
            "conflict: skipping pair %s<->%s (conflict=%s, confidence=%.2f < %.2f)",
            candidate.memory_a_id,
            candidate.memory_b_id,
            candidate.conflict,
            candidate.confidence,
            min_confidence,
        )
        return None

    from mneme.db.memories import get_memory
    from mneme.db.memory_relations import create_memory_relation
    from mneme.db.review_items import create_review_item
    from mneme.schemas.memory_relations import MemoryRelationCreate, RelationType

    # Guard: no self-referencing
    if candidate.memory_a_id == candidate.memory_b_id:
        logger.warning("conflict: skipping self-referencing pair %s", candidate.memory_a_id)
        return None

    # Validate both memories exist
    mem_a = get_memory(db, candidate.memory_a_id)
    mem_b = get_memory(db, candidate.memory_b_id)
    if mem_a is None or mem_b is None:
        logger.warning(
            "conflict: one or both memories not found (a=%s, b=%s)",
            candidate.memory_a_id,
            candidate.memory_b_id,
        )
        return None

    # Create conflicts_with relation
    try:
        relation = create_memory_relation(
            db,
            context,
            payload=MemoryRelationCreate(
                from_memory_id=candidate.memory_a_id,
                to_memory_id=candidate.memory_b_id,
                relation_type=RelationType.conflicts_with,
                reason=(
                    candidate.reason
                    or f"Auto-detected conflict (confidence={candidate.confidence:.2f})"
                ),
                metadata_json={
                    "similarity": candidate.similarity,
                    "confidence": candidate.confidence,
                    "source": "refine_conflict_llm",
                    "llm_reason": candidate.reason or "",
                    "memory_a_title": candidate.memory_a_title or "",
                    "memory_b_title": candidate.memory_b_title or "",
                },
            ),
        )
        logger.info(
            "conflict: created conflicts_with relation %s (confidence=%.2f)",
            relation.memory_relation_id,
            candidate.confidence,
        )
    except ValueError as exc:
        logger.info("conflict: relation already exists or insert failed: %s", exc)
        return None

    # Optionally create review_item
    review_item = None
    if create_review:
        try:
            review_item = create_review_item(
                project_id=mem_a.project_id,
                review_type="conflict_resolution",
                target_type="memory_relation",
                target_id=relation.memory_relation_id,
                priority=70,
                requester_actor_type=context.actor.actor_type,
                requester_actor_id=context.actor.actor_id,
                decision_payload={
                    "similarity": candidate.similarity,
                    "confidence": candidate.confidence,
                    "llm_reason": candidate.reason or "",
                    "memory_a_id": str(candidate.memory_a_id),
                    "memory_b_id": str(candidate.memory_b_id),
                    "memory_a_title": candidate.memory_a_title or "",
                    "memory_b_title": candidate.memory_b_title or "",
                },
                correlation_id=context.correlation_id,
                request_id=context.request_id,
                idempotency_key=(
                    context.idempotency_key
                    or f"conflict-review-{relation.memory_relation_id}"
                ),
            )
            logger.info(
                "conflict: created review_item %s for relation %s",
                review_item.get("review_item_id"),
                relation.memory_relation_id,
            )
        except Exception as exc:
            logger.warning("conflict: review_item creation failed (non-fatal): %s", exc)

    return {
        "memory_relation": relation,
        "review_item": review_item,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Public API — batch apply
# ═══════════════════════════════════════════════════════════════════════════

def apply_conflict_batch(
    db: Session,
    context: RequestContext,
    *,
    candidates: list[ConflictCandidate],
    min_confidence: float = 0.7,
    create_review: bool = True,
) -> ConflictResult:
    """Apply ``conflicts_with`` relations for a batch of evaluated candidates.

    Only confirmed conflicts (``conflict=True``, ``confidence >= min_confidence``)
    are written.  Each pair is independent; a single failure does not stop the batch.

    Parameters
    ----------
    db : Session
        Active SQLAlchemy session.
    context : RequestContext
        Tracing / auth context.
    candidates : list[ConflictCandidate]
        LLM-evaluated candidates from :func:`evaluate_conflicts_with_llm`.
    min_confidence : float
        Minimum LLM confidence to act (0.0-1.0).
    create_review : bool
        Whether to create review items for human confirmation.

    Returns
    -------
    ConflictResult
        Aggregated summary of what was created.
    """
    result = ConflictResult(
        candidates_found=len(candidates),
        conflicts_confirmed=sum(1 for c in candidates if c.conflict),
        candidates=candidates,
    )
    for candidate in candidates:
        outcome = apply_conflict(
            db,
            context,
            candidate=candidate,
            min_confidence=min_confidence,
            create_review=create_review,
        )
        if outcome is not None:
            result.relations_created += 1

    logger.info(
        "conflict batch: %d candidates, %d confirmed conflicts, %d relations created",
        result.candidates_found,
        result.conflicts_confirmed,
        result.relations_created,
    )
    return result


# ═══════════════════════════════════════════════════════════════════════════
# Convenience: full detect + evaluate + apply pipeline
# ═══════════════════════════════════════════════════════════════════════════

def run_conflict_pipeline(
    db: Session,
    context: RequestContext,
    *,
    project_id: UUID | None = None,
    gateway=None,
    threshold_low: float = 0.70,
    threshold_high: float = 0.92,
    max_pairs: int = 30,
    min_confidence: float = 0.7,
    dry_run: bool = False,
    create_review: bool = True,
) -> ConflictResult:
    """Full conflict detection pipeline: similarity filter -> LLM -> apply.

    Parameters
    ----------
    db : Session
        Active SQLAlchemy session.
    context : RequestContext
        Tracing / auth context.
    project_id : UUID | None
        Scope to a specific project.
    gateway : Gateway | None
        Gateway instance for LLM calls. Required unless dry_run.
    threshold_low : float
        Lower similarity bound.
    threshold_high : float
        Upper similarity bound.
    max_pairs : int
        Maximum candidate pairs.
    min_confidence : float
        Minimum LLM confidence to create relations.
    dry_run : bool
        If ``True``, skips writing relations (detection + LLM evaluation only).
    create_review : bool
        Whether to create review items.

    Returns
    -------
    ConflictResult
    """
    # Stage 1: similarity zone candidates
    result = detect_conflicts(
        db,
        project_id=project_id,
        threshold_low=threshold_low,
        threshold_high=threshold_high,
        max_pairs=max_pairs,
    )

    if not result.candidates:
        return result

    # Stage 2: LLM evaluation
    if gateway is not None:
        result = evaluate_conflicts_with_llm(
            result.candidates,
            gateway=gateway,
            project_id=project_id,
        )

    # Stage 3: apply (unless dry_run)
    if not dry_run and result.conflicts_confirmed > 0:
        result = apply_conflict_batch(
            db,
            context,
            candidates=result.candidates,
            min_confidence=min_confidence,
            create_review=create_review,
        )

    return result
