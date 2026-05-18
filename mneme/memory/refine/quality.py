"""P6-02.5 Memory Quality — compute quality scores and search weights for memories.

Scoring dimensions
------------------
::

    quality_score = weighted_avg(
        confidence_score,      # source/candidate confidence   (weight: 0.30)
        evidence_count_score,  # number of evidence spans      (weight: 0.20)
        text_coherence_score,  # LLM text quality evaluation   (weight: 0.25)
        recency_score,         # time decay factor             (weight: 0.15)
        relation_score,        # supports minus conflicts      (weight: 0.10)
    )

    search_weight = clamp(quality_score * 1.2, 0.1, 2.0)

Scores are written back to ``memories.quality_score``, ``memories.search_weight``,
and ``memory_index_entries.search_weight`` via direct SQL updates.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import exp
from typing import Any
from uuid import UUID

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from mneme.api.context import RequestContext, get_current_context

logger = logging.getLogger(__name__)

# ── Data types ────────────────────────────────────────────────────────────────


@dataclass
class QualityResult:
    """Quality score result for a single memory."""

    memory_id: UUID
    canonical_key: str
    quality_score: float
    search_weight: float
    confidence_score: float = 0.5
    evidence_count_score: float = 0.5
    text_coherence_score: float = 0.5
    recency_score: float = 0.5
    relation_score: float = 0.5
    error: str | None = None


@dataclass
class QualityBatchOutput:
    """Output of ``score_memories()``."""

    results: list[QualityResult] = field(default_factory=list)
    total_scored: int = 0
    total_failed: int = 0
    overall_stats: dict[str, float] = field(default_factory=dict)
    """Aggregate stats: min, max, avg, median quality_score."""


# ── Scoring weights ───────────────────────────────────────────────────────────

_DEFAULT_WEIGHTS = {
    "confidence": 0.30,
    "evidence": 0.20,
    "coherence": 0.25,
    "recency": 0.15,
    "relation": 0.10,
}


# ── SQL queries ───────────────────────────────────────────────────────────────

_MEMORIES_WITH_META = sql_text("""
    SELECT
      m.memory_id,
      m.canonical_key,
      m.memory_text,
      m.status,
      m.created_at,
      m.updated_at,
      m.quality_score AS current_quality_score,
      COALESCE(m.decay_score, 1.0) AS decay_score,
      COALESCE(m.emotion_charge, 'neutral') AS emotion_charge,
      COALESCE(mc.confidence_score, 0.5) AS source_confidence,
      (
        SELECT count(*)
        FROM memory_sources ms
        WHERE ms.memory_id = m.memory_id
          AND ms.memory_version = m.current_version
      ) AS evidence_span_count,
      (
        SELECT count(*)
        FROM memory_relations mr
        WHERE (mr.from_memory_id = m.memory_id OR mr.to_memory_id = m.memory_id)
          AND mr.relation_type = 'supports'
      ) AS support_count,
      (
        SELECT count(*)
        FROM memory_relations mr
        WHERE (mr.from_memory_id = m.memory_id OR mr.to_memory_id = m.memory_id)
          AND mr.relation_type = 'conflicts_with'
      ) AS conflict_count
    FROM memories m
    LEFT JOIN memory_candidates mc ON mc.candidate_id = m.activated_from_candidate_id
    WHERE (:project_id IS NULL OR m.project_id = :project_id)
      AND (:memory_id IS NULL OR m.memory_id = :memory_id)
      AND m.status IN ('active', 'draft')
    ORDER BY m.updated_at DESC
    LIMIT :limit OFFSET :offset
""")

_UPDATE_QUALITY = sql_text("""
    UPDATE memories SET
      quality_score = :qs,
      search_weight = :sw,
      last_refined_at = CURRENT_TIMESTAMP,
      updated_at = CURRENT_TIMESTAMP
    WHERE memory_id = :mid
""")

_UPDATE_INDEX_WEIGHT = sql_text("""
    UPDATE memory_index_entries SET
      search_weight = :sw,
      updated_at = CURRENT_TIMESTAMP
    WHERE memory_id = :mid
      AND fts_state = 'ready'
""")


# ── LLM prompt for coherence scoring ──────────────────────────────────────────

_COHERENCE_SYSTEM = """You are a text quality evaluator. Rate how coherent, well-structured, and
self-contained each memory entry is. Score each on a 0.0–1.0 scale:

- 0.9–1.0: Clear, well-structured, fully self-contained statement.
- 0.7–0.89: Mostly coherent, minor structural issues.
- 0.5–0.69: Understandable but fragmented or missing context.
- 0.3–0.49: Hard to follow, significant gaps.
- 0.0–0.29: Nonsense, incoherent, or empty.

Respond ONLY with a JSON array of scores matching the input order:

[{"index": 0, "score": 0.85}, {"index": 1, "score": 0.60}, ...]
"""


def _build_coherence_prompt(memories: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Build LLM messages for batch coherence scoring."""
    entries: list[str] = []
    for idx, mem in enumerate(memories):
        text = (mem.get("memory_text") or "")[:800]
        entries.append(f"[{idx}] {text}")
    user_content = (
        "Rate the coherence of each memory entry below:\n\n"
        + "\n\n---\n\n".join(entries)
    )
    return [
        {"role": "system", "content": _COHERENCE_SYSTEM},
        {"role": "user", "content": user_content},
    ]


def _call_llm_coherence(
    memories: list[dict[str, Any]],
    *,
    project_id: UUID | None = None,
    context: RequestContext | None = None,
) -> list[float]:
    """Batch-evaluate text coherence via LLM.

    Returns a list of scores (0.0–1.0) matching input order.
    Defaults to 0.5 for failed entries.
    """
    default = [0.5] * len(memories)
    if not memories:
        return []

    try:
        from mneme.gateway.call import Gateway

        gw = Gateway()
        ctx = context or get_current_context()
        messages = _build_coherence_prompt(memories)
        result = gw.call(
            capability_code="chat.completion",
            params={
                "messages": messages,
                "temperature": 0.1,
                "max_tokens": 1024,
            },
            project_id=project_id,
            sensitivity="private",
            actor_type=ctx.actor.actor_type,
            actor_id=ctx.actor.actor_id,
            request_id=ctx.request_id,
            correlation_id=ctx.correlation_id,
        )

        data = result.get("data", {})
        content = ""
        if isinstance(data, dict):
            choices = data.get("choices", [])
            if choices:
                content = str(choices[0].get("message", {}).get("content", ""))
            else:
                content = str(data.get("content", "") or data.get("text", ""))

        if not content:
            logger.warning("Empty LLM coherence response")
            return default

        content = content.strip()
        if content.startswith("```"):
            lines = content.split("\n")
            lines = [ln for ln in lines if not ln.startswith("```")]
            content = "\n".join(lines).strip()

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            brace = content.find("[")
            bracket = content.rfind("]") + 1
            if brace >= 0 and bracket > brace:
                parsed = json.loads(content[brace:bracket])
            else:
                logger.warning("Failed to parse coherence JSON")
                return default

        if not isinstance(parsed, list):
            return default

        scores_map: dict[int, float] = {}
        for item in parsed:
            if isinstance(item, dict) and "index" in item and "score" in item:
                idx = int(item["index"])
                s = float(item["score"])
                scores_map[idx] = max(0.0, min(1.0, s))

        return [scores_map.get(i, 0.5) for i in range(len(memories))]

    except Exception:
        logger.exception("LLM coherence call failed")
        return default


# ── Individual scoring functions ──────────────────────────────────────────────


def _compute_evidence_score(span_count: int) -> float:
    """Map evidence span count to 0.0–1.0."""
    if span_count <= 0:
        return 0.2
    if span_count >= 10:
        return 1.0
    return 0.2 + 0.8 * (span_count / 10.0)


def _compute_recency_score(
    created_at: datetime | None,
    updated_at: datetime | None,
    *,
    decay_score: float = 1.0,
    half_life_days: float = 90.0,
    time_weight: float = 0.4,
) -> float:
    """Time-decay score blending time-based recency with system ``decay_score``.

    Two components:
      1. *Time recency*: exponential decay based on ``updated_at``/``created_at`` age.
      2. *Decay score*:  the system-managed ``decay_score`` from the Ebbinghaus decay
         engine (which already incorporates emotion modulation and reinforcement).

    The blended score is a weighted average of the two, controlled by
    ``time_weight`` (default 0.4, giving 60% weight to the behaviour-derived
    decay_score and 40% to raw time).
    """
    # ── Time-based component ──────────────────────────────────────────────
    ref = updated_at or created_at
    if ref is None:
        time_recency = 0.5
    else:
        now_dt = datetime.now(timezone.utc)
        age_days = (now_dt - ref).total_seconds() / 86400.0
        if age_days <= 0:
            time_recency = 1.0
        else:
            time_recency = exp(-age_days / half_life_days)

    # ── Blended score: time recency × weight + decay_score × (1 - weight) ──
    return time_recency * time_weight + decay_score * (1.0 - time_weight)


def _compute_relation_score(support_count: int, conflict_count: int) -> float:
    """Score based on support vs conflict relations (0.0–1.0)."""
    total = support_count + conflict_count
    if total == 0:
        return 0.5
    ratio = support_count / total
    return 0.5 + 0.5 * (2.0 * ratio - 1.0)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


# ── Main scoring pipeline ─────────────────────────────────────────────────────


def score_memories(
    db: Session,
    *,
    project_id: UUID | None = None,
    memory_ids: list[UUID] | None = None,
    context: RequestContext | None = None,
    weights: dict[str, float] | None = None,
    batch_size: int = 10,
    max_memories: int = 200,
) -> QualityBatchOutput:
    """Batch score memories and write results to ``memories`` + ``memory_index_entries``.

    Parameters
    ----------
    db : Session
        Active SQLAlchemy session.
    project_id : UUID | None
        Limit to one project.  ``None`` = all projects.
    memory_ids : list[UUID] | None
        Specific memory IDs to score.  Overrides pagination.
    context : RequestContext | None
        Request context for Gateway LLM calls.
    weights : dict[str, float] | None
        Custom dimension weights.  Defaults to ``_DEFAULT_WEIGHTS``.
    batch_size : int
        Memories per LLM coherence batch.
    max_memories : int
        Maximum total memories to score.

    Returns
    -------
    QualityBatchOutput
    """
    w = weights or _DEFAULT_WEIGHTS
    output = QualityBatchOutput()

    # 1. Fetch memory metadata
    all_memories: list[dict[str, Any]] = []

    if memory_ids:
        for mid in memory_ids:
            rows = db.execute(
                _MEMORIES_WITH_META,
                {
                    "project_id": project_id,
                    "memory_id": mid,
                    "limit": 1,
                    "offset": 0,
                },
            ).all()
            for row in rows:
                all_memories.append(dict(row._mapping))
    else:
        offset = 0
        while len(all_memories) < max_memories:
            rows = db.execute(
                _MEMORIES_WITH_META,
                {
                    "project_id": project_id,
                    "memory_id": None,
                    "limit": max_memories,
                    "offset": offset,
                },
            ).all()
            if not rows:
                break
            for row in rows:
                all_memories.append(dict(row._mapping))
            offset += len(rows)

    if not all_memories:
        return output

    # 2. Batch LLM coherence scores
    coherence_scores: list[float] = []
    for i in range(0, len(all_memories), batch_size):
        batch = all_memories[i : i + batch_size]
        scores = _call_llm_coherence(
            batch, project_id=project_id, context=context
        )
        coherence_scores.extend(scores)

    # Ensure length match
    while len(coherence_scores) < len(all_memories):
        coherence_scores.append(0.5)

    # 3. Compute per-memory scores
    for idx, mem in enumerate(all_memories):
        try:
            mid = mem["memory_id"]
            ckey = mem["canonical_key"]

            confidence = float(mem.get("source_confidence", 0.5))
            confidence = _clamp(confidence, 0.0, 1.0)

            evidence = _compute_evidence_score(int(mem.get("evidence_span_count", 0)))

            coherence = coherence_scores[idx] if idx < len(coherence_scores) else 0.5

            decay_val = float(mem.get("decay_score", 1.0))
            recency = _compute_recency_score(
                mem.get("created_at"),
                mem.get("updated_at"),
                decay_score=decay_val,
            )

            relation = _compute_relation_score(
                int(mem.get("support_count", 0)),
                int(mem.get("conflict_count", 0)),
            )

            quality = (
                w["confidence"] * confidence
                + w["evidence"] * evidence
                + w["coherence"] * coherence
                + w["recency"] * recency
                + w["relation"] * relation
            )
            quality = _clamp(quality, 0.0, 1.0)
            search_weight = _clamp(quality * 1.2, 0.1, 2.0)

            # Write to DB
            db.execute(
                _UPDATE_QUALITY,
                {"qs": quality, "sw": search_weight, "mid": mid},
            )
            db.execute(
                _UPDATE_INDEX_WEIGHT,
                {"sw": search_weight, "mid": mid},
            )

            output.results.append(
                QualityResult(
                    memory_id=mid,
                    canonical_key=ckey,
                    quality_score=round(quality, 4),
                    search_weight=round(search_weight, 4),
                    confidence_score=round(confidence, 4),
                    evidence_count_score=round(evidence, 4),
                    text_coherence_score=round(coherence, 4),
                    recency_score=round(recency, 4),
                    relation_score=round(relation, 4),
                )
            )
            output.total_scored += 1

        except Exception as exc:
            logger.exception("Failed to score memory %s", mem.get("memory_id"))
            output.total_failed += 1
            output.results.append(
                QualityResult(
                    memory_id=mem["memory_id"],
                    canonical_key=mem.get("canonical_key", "unknown"),
                    quality_score=0.0,
                    search_weight=1.0,
                    error=str(exc),
                )
            )

    # 4. Compute aggregate stats
    scores = [r.quality_score for r in output.results if r.error is None]
    if scores:
        scores_sorted = sorted(scores)
        n = len(scores_sorted)
        median = (
            scores_sorted[n // 2]
            if n % 2 == 1
            else (scores_sorted[n // 2 - 1] + scores_sorted[n // 2]) / 2.0
        )
        output.overall_stats = {
            "min": round(min(scores), 4),
            "max": round(max(scores), 4),
            "avg": round(sum(scores) / len(scores), 4),
            "median": round(median, 4),
        }

    logger.info(
        "Quality scoring complete: %d scored, %d failed, stats=%s",
        output.total_scored,
        output.total_failed,
        output.overall_stats,
    )
    return output


def apply_quality_scores(
    db: Session,
    *,
    results: list[QualityResult],
) -> int:
    """Write pre-computed quality scores to ``memories`` + ``memory_index_entries``.

    Standalone bulk-apply function for externally-computed scores.

    Returns the number of rows updated.
    """
    updated = 0
    for r in results:
        if r.error:
            continue
        try:
            db.execute(
                _UPDATE_QUALITY,
                {"qs": r.quality_score, "sw": r.search_weight, "mid": r.memory_id},
            )
            db.execute(
                _UPDATE_INDEX_WEIGHT,
                {"sw": r.search_weight, "mid": r.memory_id},
            )
            updated += 1
        except Exception:
            logger.exception("Failed to apply quality score for %s", r.memory_id)
    return updated
