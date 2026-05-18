"""P4-12 Emotion Inference Engine — behavior-based, no manual annotation.

Infers emotion_charge (neutral/embarrassed/proud/fearful) from behavioral
signals embedded in memory content and interaction patterns:

* **Linguistic analysis**: keyword/heuristic scanning of memory_text
* **Interaction patterns**: reinforcement frequency, version churn,
  time since creation
* **Decay linkage**: emotion modulates decay_rate — proud memories fade
  slower, embarrassing ones faster, fearful ones persist longest.

uncertainty_score (0.0–1.0) quantifies how confident the inference engine
is about its classification. Higher = less confident.

Design principle
----------------
Emotion is NEVER manually annotated. It is ALWAYS inferred from observable
behavior (text content + system interaction patterns). This keeps the system
objective and prevents human bias in emotional labeling.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session

from mneme.config import get_settings

logger = logging.getLogger(__name__)


# ── Emotion charge enum ──────────────────────────────────────────────────────

EMOTION_CHARGES = ("neutral", "embarrassed", "proud", "fearful")


# ══════════════════════════════════════════════════════════════════════════════
# Keyword / Pattern Lexicons (behavior-inferred, not manually curated per-item)
# ══════════════════════════════════════════════════════════════════════════════

# ── Proud indicators ────────────────────────────────────────────────────────
_PROUD_PATTERNS: list[re.Pattern] = [
    re.compile(r"\b(proud|achievement|accomplish|success|milestone|victory"
              r"|excel|master|conquer|triumph|celebrate|breakthrough)\b", re.I),
    re.compile(r"\b(did (a )?(great|fantastic|amazing|excellent|outstanding) job)\b", re.I),
    re.compile(r"\b(finally (figured|solved|mastered|learned|understood))\b", re.I),
    re.compile(r"\b(nailed it|cracked it|made it|got it right)\b", re.I),
    re.compile(r"\b(proud of|so proud|feeling proud)\b", re.I),
]

# ── Embarrassed indicators ───────────────────────────────────────────────────
_EMBARRASSED_PATTERNS: list[re.Pattern] = [
    re.compile(r"\b(embarrass|ashamed|cringe|humiliat|mortif|awkward"
              r"|blunder|faux pas|facepalm|oops|regret|stupid mistake)\b", re.I),
    re.compile(r"\b(can'?t believe I (said|did|forgot|missed))\b", re.I),
    re.compile(r"\b(wish I hadn'?t|shouldn'?t have|why did I)\b", re.I),
    re.compile(r"\b(so embarrassing|how embarrassing|that was awkward)\b", re.I),
    re.compile(r"\b(messed up|screwed up|f\*+cked up)\b", re.I),
]

# ── Fearful indicators ───────────────────────────────────────────────────────
_FEARFUL_PATTERNS: list[re.Pattern] = [
    re.compile(r"\b(afraid|fear|scared|terrif|frighten|panic|anxious"
              r"|anxiety|dread|alarm|horror|threat|danger|risk|worry"
              r"|concern|uneasy|nervous|paranoi)\b", re.I),
    re.compile(r"\b(what if|might go wrong|could fail|worst case)\b", re.I),
    re.compile(r"\b(too risky|playing with fire|dangerous)\b", re.I),
    re.compile(r"\b(not safe|unsafe|vulnerable|exposed)\b", re.I),
    re.compile(r"\b(stress(ed|ful)?|overwhelm(ed|ing)?|can'?t handle)\b", re.I),
]

# ── Neutral patterns (explicitly neutral language) ───────────────────────────
_NEUTRAL_PATTERNS: list[re.Pattern] = [
    re.compile(r"\b(summar|overview|recap|note to self|reminder|reference"
              r"|documentation|log|record|entry)\b", re.I),
    re.compile(r"\b(factual|objective|neutral|impartial)\b", re.I),
]


# ── Uncertainty indicators ──────────────────────────────────────────────────
_UNCERTAINTY_MARKERS: list[re.Pattern] = [
    re.compile(r"\b(maybe|perhaps|possibly|I think|I guess|probably"
              r"|might be|could be|not sure|uncertain|unclear|ambiguous)\b", re.I),
    re.compile(r"\b(not entirely sure|don'?t know for sure|speculat)\b", re.I),
]


# ══════════════════════════════════════════════════════════════════════════════
# Inference Engine
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class EmotionInference:
    """Result of emotion inference for a single memory."""

    emotion_charge: str  # 'neutral' | 'embarrassed' | 'proud' | 'fearful'
    uncertainty_score: float  # 0.0 (certain) – 1.0 (pure guess)
    signal_strengths: dict[str, float] = field(default_factory=dict)
    dominant_signals: list[str] = field(default_factory=list)


def _count_matches(text: str, patterns: list[re.Pattern]) -> int:
    """Count total regex matches across all patterns in *text*."""
    total = 0
    for pat in patterns:
        total += len(pat.findall(text))
    return total


def _count_uncertainty(text: str) -> int:
    """Count uncertainty markers in the text."""
    return _count_matches(text, _UNCERTAINTY_MARKERS)


def infer_emotion_from_text(
    memory_text: str,
    *,
    reinforcement_count: int = 0,
    version_count: int = 1,
    days_since_created: float = 0.0,
) -> EmotionInference:
    """Infer emotion_charge and uncertainty_score from behavioral signals.

    Parameters
    ----------
    memory_text : str
        The full memory content text.
    reinforcement_count : int
        Number of times this memory has been reinforced (search hits, recalls).
    version_count : int
        Number of versions (edits). High churn may indicate uncertainty.
    days_since_created : float
        Days since memory creation. Very old + unreinforced = likely neutral.

    Returns
    -------
    EmotionInference
        Inferred emotion, uncertainty, and diagnostic signal strengths.
    """
    settings = get_settings()

    # ── Count keyword matches per emotion category ──────────────────────
    proud_hits = _count_matches(memory_text, _PROUD_PATTERNS)
    embarrassed_hits = _count_matches(memory_text, _EMBARRASSED_PATTERNS)
    fearful_hits = _count_matches(memory_text, _FEARFUL_PATTERNS)
    neutral_hits = _count_matches(memory_text, _NEUTRAL_PATTERNS)

    # ── Behavioral adjustments ──────────────────────────────────────────
    # High reinforcement → signal amplification (memory is important)
    boost = 1.0 + min(reinforcement_count * 0.1, 0.5)  # cap at +50%

    # Very old memories with no reinforcement → bias toward neutral
    age_neutral_bias = 0.0
    if days_since_created > 30 and reinforcement_count == 0:
        age_neutral_bias = min((days_since_created - 30) / 60, 0.4)

    # High version churn → bias toward uncertainty
    version_uncertainty_factor = min((version_count - 1.0) * 0.1, 0.3)

    # ── Compute weighted scores ─────────────────────────────────────────
    scores = {
        "proud": proud_hits * boost,
        "embarrassed": embarrassed_hits * boost,
        "fearful": fearful_hits * boost,
        "neutral": (neutral_hits * boost) + age_neutral_bias * 3.0,
    }

    total_signal = sum(scores.values())

    # ── Determine emotion_charge ────────────────────────────────────────
    if total_signal < settings.emotion_min_signal_threshold:
        # Not enough signal — default to neutral with high uncertainty
        return EmotionInference(
            emotion_charge="neutral",
            uncertainty_score=min(0.8 + version_uncertainty_factor, 1.0),
            signal_strengths=scores,
            dominant_signals=["insufficient_signal"],
        )

    # Pick the emotion with the highest score
    best_emotion = max(scores, key=scores.get)
    best_score = scores[best_emotion]

    # ── Compute uncertainty_score ───────────────────────────────────────
    # Factors that increase uncertainty:
    # 1. Text contains uncertainty markers
    # 2. Multiple emotions have similar signal strength (ambiguity)
    # 3. Low total signal
    # 4. High version churn

    uncertainty_markers = _count_uncertainty(memory_text)

    # Ambiguity: ratio of second-best to best
    sorted_scores = sorted(scores.values(), reverse=True)
    ambiguity_ratio = (sorted_scores[1] / best_score) if best_score > 0 and len(sorted_scores) > 1 else 0.0

    # Base uncertainty from signal strength
    base_uncertainty = max(0.0, 1.0 - (total_signal / settings.emotion_strong_signal_threshold))
    base_uncertainty = min(base_uncertainty, 1.0)

    # Combine factors
    raw_uncertainty = (
        base_uncertainty * 0.35 +
        ambiguity_ratio * 0.25 +
        min(uncertainty_markers * 0.08, 0.2) +
        version_uncertainty_factor * 0.2
    )

    uncertainty = min(max(raw_uncertainty, 0.05), 1.0)  # floor at 0.05, cap at 1.0

    # ── Signal strengths (normalised for diagnostics) ───────────────────
    norm_scores = {}
    if total_signal > 0:
        for k, v in scores.items():
            norm_scores[k] = round(v / total_signal, 4)

    # Determine dominant signals (any category with >= 20% of total)
    dominant = [k for k, v in norm_scores.items() if v >= 0.2]

    return EmotionInference(
        emotion_charge=best_emotion,
        uncertainty_score=round(uncertainty, 4),
        signal_strengths=norm_scores,
        dominant_signals=dominant or [best_emotion],
    )


# ══════════════════════════════════════════════════════════════════════════════
# Decay Rate Modulation
# ══════════════════════════════════════════════════════════════════════════════

# Emotion → decay rate multiplier
# Proud memories persist (reinforce self-image), fearful stick (survival),
# embarrassing fade fast (psychological protection), neutral = baseline.
EMOTION_DECAY_MULTIPLIERS: dict[str, float] = {
    "neutral": 1.0,
    "proud": 0.7,       # 30% slower decay — positive memories endure
    "embarrassed": 1.3, # 30% faster decay — let's forget the cringe
    "fearful": 0.5,     # 50% slower decay — threat memories persist
}


def get_emotion_decay_multiplier(emotion_charge: str) -> float:
    """Return the decay rate multiplier for a given emotion_charge.

    Multipliers are applied to the base decay_rate_per_day when computing
    time-decay. A multiplier < 1.0 means slower decay (memory persists longer).
    """
    return EMOTION_DECAY_MULTIPLIERS.get(emotion_charge, 1.0)


# ══════════════════════════════════════════════════════════════════════════════
# Database Operations — Batch Inference
# ══════════════════════════════════════════════════════════════════════════════

_BATCH_INFER_QUERY = text("""
    SELECT
        m.memory_id, m.canonical_key, m.memory_text,
        m.current_version, m.decay_score, m.decay_state,
        m.emotion_charge, m.uncertainty_score,
        m.created_at,
        COALESCE(rc.reinforcement_count, 0) AS reinforcement_count
    FROM memories m
    LEFT JOIN LATERAL (
        SELECT COUNT(*) AS reinforcement_count
        FROM memory_versions mv
        WHERE mv.memory_id = m.memory_id
          AND mv.action = 'reinforce'
    ) rc ON true
    WHERE m.status = 'active'
      AND (
          m.last_emotion_inferred_at IS NULL
          OR m.last_emotion_inferred_at < NOW() - INTERVAL '1 day'
          OR m.uncertainty_score > :reinf_threshold
      )
    ORDER BY m.last_emotion_inferred_at ASC NULLS FIRST
    LIMIT :limit
""")


_UPDATE_EMOTION = text("""
    UPDATE memories
    SET emotion_charge = :emotion,
        uncertainty_score = :uncertainty,
        last_emotion_inferred_at = :now,
        updated_at = now()
    WHERE memory_id = :mid
    RETURNING memory_id, canonical_key, emotion_charge, uncertainty_score
""")


@dataclass
class EmotionInferResult:
    """Result of applying emotion inference to a batch of memories."""

    total_processed: int = 0
    emotions_updated: int = 0
    emotion_counts: dict[str, int] = field(default_factory=dict)
    avg_uncertainty: float = 0.0
    errors: list[str] = field(default_factory=list)


def apply_emotion_inference_batch(
    db: Session,
    *,
    limit: int | None = None,
) -> EmotionInferResult:
    """Infer emotion_charge for a batch of active memories.

    Fetches memories that haven't been inferred recently (or have high
    uncertainty), runs the inference engine, and updates the DB.

    Parameters
    ----------
    db : Session
        Active database session.
    limit : int or None
        Max memories to process. Defaults to ``settings.emotion_infer_batch_size``.

    Returns
    -------
    EmotionInferResult
        Summary of processed and updated emotions.
    """
    settings = get_settings()
    if limit is None:
        limit = settings.emotion_infer_batch_size

    now = datetime.now(timezone.utc)
    result = EmotionInferResult()

    rows = db.execute(
        _BATCH_INFER_QUERY,
        {
            "limit": limit,
            "reinf_threshold": settings.emotion_reinfer_uncertainty_threshold,
        },
    ).all()

    if not rows:
        return result

    total_uncertainty = 0.0

    for row in rows:
        row_data = dict(row._mapping)
        memory_id: UUID = row_data["memory_id"]
        canonical_key: str = row_data["canonical_key"]
        memory_text: str = row_data["memory_text"]
        version_count: int = int(row_data["current_version"])
        reinforcement_count: int = int(row_data["reinforcement_count"])
        created_at: datetime | None = row_data["created_at"]

        days_since_created = 0.0
        if created_at is not None:
            days_since_created = (now - created_at).total_seconds() / 86400.0

        try:
            inference = infer_emotion_from_text(
                memory_text=memory_text,
                reinforcement_count=reinforcement_count,
                version_count=version_count,
                days_since_created=days_since_created,
            )

            old_emotion = row_data.get("emotion_charge", "neutral")

            # Only update if emotion changed or uncertainty significantly different
            if (
                inference.emotion_charge != old_emotion
                or abs(inference.uncertainty_score - float(row_data.get("uncertainty_score", 0.5))) > 0.1
            ):
                db.execute(
                    _UPDATE_EMOTION,
                    {
                        "emotion": inference.emotion_charge,
                        "uncertainty": inference.uncertainty_score,
                        "now": now,
                        "mid": memory_id,
                    },
                )
                result.emotions_updated += 1

            result.total_processed += 1
            result.emotion_counts[inference.emotion_charge] = (
                result.emotion_counts.get(inference.emotion_charge, 0) + 1
            )
            total_uncertainty += inference.uncertainty_score

        except Exception as e:
            error_msg = f"memory {memory_id} ({canonical_key}): {e}"
            logger.error("emotion inference failed for %s: %s", canonical_key, e)
            result.errors.append(error_msg)

    if result.total_processed > 0:
        result.avg_uncertainty = round(total_uncertainty / result.total_processed, 4)

    # Commit changes
    if result.emotions_updated > 0:
        db.commit()
        logger.info(
            "emotion inference batch complete — processed=%d updated=%d "
            "counts=%s avg_uncertainty=%.3f errors=%d",
            result.total_processed,
            result.emotions_updated,
            result.emotion_counts,
            result.avg_uncertainty,
            len(result.errors),
        )

    return result


def get_emotion_status(
    db: Session,
    *,
    project_id: UUID | None = None,
) -> dict[str, Any]:
    """Get emotion distribution summary for a project (or globally).

    Returns counts by emotion_charge and average uncertainty_score.
    """
    rows = db.execute(
        text("""
            SELECT
                emotion_charge,
                COUNT(*) as cnt,
                AVG(uncertainty_score)::numeric(5,4) as avg_uncertainty
            FROM memories
            WHERE (:pid IS NULL OR project_id = :pid)
              AND status = 'active'
            GROUP BY emotion_charge
            ORDER BY
                CASE emotion_charge
                    WHEN 'neutral' THEN 1
                    WHEN 'proud' THEN 2
                    WHEN 'embarrassed' THEN 3
                    WHEN 'fearful' THEN 4
                END
        """),
        {"pid": project_id},
    ).all()

    status: dict[str, Any] = {
        "project_id": str(project_id) if project_id else None,
        "total_active": 0,
        "total_neutral": 0,
        "total_proud": 0,
        "total_embarrassed": 0,
        "total_fearful": 0,
        "avg_uncertainty": 0.0,
    }

    total_count = 0
    total_weighted_uncertainty = 0.0

    for row in rows:
        row_data = dict(row._mapping)
        emotion = str(row_data["emotion_charge"])
        count = int(row_data["cnt"])
        avg_unc = float(row_data["avg_uncertainty"]) if row_data["avg_uncertainty"] is not None else 0.5

        key_map = {
            "neutral": "total_neutral",
            "proud": "total_proud",
            "embarrassed": "total_embarrassed",
            "fearful": "total_fearful",
        }
        if emotion in key_map:
            status[key_map[emotion]] = count

        total_count += count
        total_weighted_uncertainty += avg_unc * count

    status["total_active"] = total_count
    if total_count > 0:
        status["avg_uncertainty"] = round(total_weighted_uncertainty / total_count, 4)

    return status
