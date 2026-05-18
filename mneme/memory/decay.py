"""P4-11 Memory Time Decay Engine.

Implements the Ebbinghaus-style time decay for memories:

* **Decay**: decay_score decreases linearly over time based on ``decay_rate_per_day``.
* **Reinforce**: memory access/search hit/LLM recall adds a reinforcement bonus
  to decay_score (capped at 1.0).
* **Threshold action**: decay_state transitions automatically:
  active (decay_score >= active_threshold) → decaying → silent → archived (< archive_threshold).
* **Idempotent**: running decay multiple times on the same memory within a short
  period is safe — the calculation is time-delta based.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session

from mneme.api.context import RequestContext
from mneme.config import get_settings
from mneme.db.audit import (
    AuditEvent,
    OutboxEvent,
    write_with_audit_outbox_idempotency,
)
from mneme.schemas.memories import DecayStateTransition

logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────

SECONDS_PER_DAY = 86400.0


# ── Threshold resolver ───────────────────────────────────────────────────────

def _decay_state_from_score(score: float, settings=None) -> str:
    """Map a decay_score to the appropriate decay_state string."""
    if settings is None:
        settings = get_settings()
    if score >= settings.decay_active_threshold:
        return "active"
    elif score >= settings.decay_silent_threshold:
        return "decaying"
    elif score >= settings.decay_archive_threshold:
        return "silent"
    else:
        return "archived"


# ── Core decay calculation ───────────────────────────────────────────────────

def compute_decay(
    *,
    current_score: float,
    last_decayed_at: datetime | None,
    now: datetime | None = None,
    decay_rate_per_day: float | None = None,
    emotion_charge: str = "neutral",
) -> float:
    """Compute the new decay_score based on elapsed time.

    Parameters
    ----------
    current_score : float
        Current decay_score (0.0-1.0).
    last_decayed_at : datetime or None
        When the memory was last decayed. If None, uses current_score unchanged
        (treats as just-created).
    now : datetime or None
        Current timestamp. Defaults to ``datetime.now(timezone.utc)``.
    decay_rate_per_day : float or None
        Rate per 24h. Defaults to ``settings.decay_rate_per_day``.
    emotion_charge : str
        Emotion charge of the memory ('neutral', 'embarrassed', 'proud', 'fearful').
        Modulates the effective decay rate via EMOTION_DECAY_MULTIPLIERS.

    Returns
    -------
    float
        New decay_score, clamped to [0.0, 1.0].
    """
    if decay_rate_per_day is None:
        decay_rate_per_day = get_settings().decay_rate_per_day
    if now is None:
        now = datetime.now(timezone.utc)

    if last_decayed_at is None:
        # Never decayed — treat as fresh (no decay applied this cycle)
        return current_score

    elapsed_seconds = (now - last_decayed_at).total_seconds()
    if elapsed_seconds <= 0:
        return current_score

    # Apply emotion multiplier to decay rate
    from mneme.memory.emotion import get_emotion_decay_multiplier
    effective_rate = decay_rate_per_day * get_emotion_decay_multiplier(emotion_charge)

    # Linear decay: score -= effective_rate * (elapsed_days)
    elapsed_days = elapsed_seconds / SECONDS_PER_DAY
    decay_amount = effective_rate * elapsed_days
    new_score = current_score - decay_amount

    return max(0.0, min(1.0, new_score))


def compute_reinforce(
    *,
    current_score: float,
    bonus: float,
) -> float:
    """Add a reinforcement bonus to decay_score, capped at 1.0.

    Parameters
    ----------
    current_score : float
        Current decay_score (0.0-1.0).
    bonus : float
        Bonus to add (0.0-1.0), typically from settings.decay_reinforcement_bonus.

    Returns
    -------
    float
        New decay_score, clamped to [0.0, 1.0].
    """
    return min(1.0, current_score + bonus)


# ── Database operations ──────────────────────────────────────────────────────

_UPDATE_DECAY = text("""
    UPDATE memories
    SET decay_score = :score,
        decay_state = :state,
        last_decayed_at = :now,
        updated_at = now()
    WHERE memory_id = :mid
    RETURNING memory_id, canonical_key, decay_score, decay_state
""")

_BATCH_DECAY_QUERY = text("""
    SELECT memory_id, canonical_key, decay_score, last_decayed_at, emotion_charge
    FROM memories
    WHERE status = 'active'
      AND decay_state != 'archived'
    ORDER BY last_decayed_at ASC NULLS FIRST
    LIMIT :limit
""")

_GET_DECAY_STATUS = text("""
    SELECT
        decay_state,
        COUNT(*) as cnt,
        AVG(decay_score)::numeric(5,4) as avg_score
    FROM memories
    WHERE (:pid IS NULL OR project_id = :pid)
      AND status = 'active'
    GROUP BY decay_state
    ORDER BY
        CASE decay_state
            WHEN 'active' THEN 1
            WHEN 'decaying' THEN 2
            WHEN 'silent' THEN 3
            WHEN 'archived' THEN 4
        END
""")


@dataclass
class DecayResult:
    """Result of applying decay to a batch of memories."""

    total_processed: int = 0
    transitions: list[DecayStateTransition] = field(default_factory=list)
    scores_updated: int = 0
    errors: list[str] = field(default_factory=list)


def apply_decay_batch(
    db: Session,
    *,
    limit: int | None = None,
) -> DecayResult:
    """Apply time-decay to a batch of active memories.

    Fetches up to ``limit`` active memories ordered by least-recently-decayed,
    computes the elapsed decay, updates decay_score and decay_state.

    Parameters
    ----------
    db : Session
        Active database session.
    limit : int or None
        Max memories to process. Defaults to ``settings.decay_max_batch_size``.

    Returns
    -------
    DecayResult
        Summary of processed and transitioned memories.
    """
    settings = get_settings()
    if limit is None:
        limit = settings.decay_max_batch_size

    now = datetime.now(timezone.utc)
    result = DecayResult()

    rows = db.execute(
        _BATCH_DECAY_QUERY,
        {"limit": limit},
    ).all()

    if not rows:
        return result

    for row in rows:
        row_data = dict(row._mapping)
        memory_id: UUID = row_data["memory_id"]
        canonical_key: str = row_data["canonical_key"]
        current_score: float = float(row_data["decay_score"])
        last_decayed_at: datetime | None = row_data["last_decayed_at"]
        emotion_charge: str = str(row_data.get("emotion_charge", "neutral"))

        try:
            old_state = _decay_state_from_score(current_score, settings)

            # Compute new score (emotion modulates decay rate)
            new_score = compute_decay(
                current_score=current_score,
                last_decayed_at=last_decayed_at,
                now=now,
                decay_rate_per_day=settings.decay_rate_per_day,
                emotion_charge=emotion_charge,
            )

            new_state = _decay_state_from_score(new_score, settings)

            # Only update if score actually changed
            if abs(new_score - current_score) < 0.0001:
                continue

            db.execute(
                _UPDATE_DECAY,
                {
                    "score": new_score,
                    "state": new_state,
                    "now": now,
                    "mid": memory_id,
                },
            )
            result.scores_updated += 1

            # Track state transitions
            if new_state != old_state:
                result.transitions.append(
                    DecayStateTransition(
                        memory_id=memory_id,
                        canonical_key=canonical_key,
                        from_state=old_state,
                        to_state=new_state,
                        decay_score=round(new_score, 4),
                    )
                )

            result.total_processed += 1

        except Exception as e:
            error_msg = f"memory {memory_id}: {e}"
            logger.error("decay failed for %s: %s", canonical_key, e)
            result.errors.append(error_msg)

    # Commit all changes
    if result.scores_updated > 0:
        db.commit()
        logger.info(
            "decay batch complete — processed=%d updated=%d transitions=%d errors=%d",
            result.total_processed,
            result.scores_updated,
            len(result.transitions),
            len(result.errors),
        )

    return result


def reinforce_memory(
    db: Session,
    context: RequestContext,
    *,
    memory_id: UUID,
    bonus: float | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Apply a reinforcement bonus to a specific memory.

    Used when a memory is accessed (search hit, LLM recall, explicit pin).
    Adds *bonus* to decay_score (capped at 1.0) and resets decay_state if needed.
    Updates ``last_reinforced_at``.
    Records a version row (action='reinforce').

    Parameters
    ----------
    db : Session
        Active database session.
    context : RequestContext
        Request context for audit/outbox.
    memory_id : UUID
        Target memory.
    bonus : float or None
        Reinforcement bonus. Defaults to ``settings.decay_reinforcement_bonus``.
    reason : str or None
        Why this reinforcement happened (e.g. 'search_hit', 'llm_recall').

    Returns
    -------
    dict
        Keys: memory_id, canonical_key, decay_score, decay_state, old_score, old_state.
    """
    settings = get_settings()
    if bonus is None:
        bonus = settings.decay_reinforcement_bonus

    # Fetch current memory
    from mneme.db.memories import get_memory
    memory = get_memory(db, memory_id)
    if memory is None:
        raise ValueError(f"memory {memory_id} not found")

    old_score = memory.decay_score
    old_state = memory.decay_state
    new_score = compute_reinforce(current_score=old_score, bonus=bonus)
    new_state = _decay_state_from_score(new_score, settings)
    now = datetime.now(timezone.utc)

    new_version = memory.current_version + 1
    before_snapshot = {
        "decay_score": old_score,
        "decay_state": old_state,
    }
    after_snapshot = {
        "decay_score": new_score,
        "decay_state": new_state,
    }

    outbox_event = OutboxEvent(
        event_type="memory.reinforced",
        aggregate_type="memory",
        aggregate_id=memory_id,
        aggregate_version=new_version,
        idempotency_key=f"{context.idempotency_key or ''}:reinforce:{memory_id}",
        producer="mneme-api",
        payload_json={
            "memory_id": str(memory_id),
            "old_score": old_score,
            "new_score": new_score,
            "bonus": bonus,
            "reason": reason,
        },
    )

    audit_event = AuditEvent(
        action="memory.reinforce",
        result="success",
        object_type="memory",
        object_id=memory_id,
        project_id=memory.project_id,
        sensitivity_level=memory.sensitivity_level,
        diff_summary={
            "decay_score": f"{old_score:.4f}→{new_score:.4f}",
            "decay_state": f"{old_state}→{new_state}",
            "reason": reason,
        },
    )

    def _do_reinforce(db: Session) -> dict[str, Any]:
        row = db.execute(
            text("""
                UPDATE memories
                SET decay_score = :score,
                    decay_state = :state,
                    last_reinforced_at = :now,
                    last_decayed_at = :now,
                    current_version = :ver,
                    updated_at = now()
                WHERE memory_id = :mid
                  AND current_version = :ver - 1
                RETURNING memory_id, canonical_key, decay_score, decay_state, current_version
            """),
            {
                "score": new_score,
                "state": new_state,
                "now": now,
                "ver": new_version,
                "mid": memory_id,
            },
        ).first()

        if row is None:
            raise ValueError(f"concurrent update on memory {memory_id}")

        row_data = dict(row._mapping)

        # Record version
        from mneme.db.memories import _record_memory_version
        _record_memory_version(
            db,
            memory_id=memory_id,
            version=new_version,
            action="reinforce",
            before_json=before_snapshot,
            after_json=after_snapshot,
            actor_type=context.actor.actor_type,
            actor_id=context.actor.actor_id,
            reason=reason,
        )

        return {
            "memory_id": str(memory_id),
            "canonical_key": str(row_data["canonical_key"]),
            "decay_score": float(row_data["decay_score"]),
            "decay_state": str(row_data["decay_state"]),
            "old_score": old_score,
            "old_state": old_state,
            "current_version": int(row_data["current_version"]),
        }

    def _resolve_existing(_db: Session, _aggregate_id: UUID) -> dict[str, Any]:
        mem = get_memory(_db, _aggregate_id)
        if mem is None:
            raise LookupError(f"memory {_aggregate_id} not found")
        return {
            "memory_id": str(mem.memory_id),
            "canonical_key": mem.canonical_key,
            "decay_score": mem.decay_score,
            "decay_state": mem.decay_state,
            "old_score": old_score,
            "old_state": old_state,
        }

    return write_with_audit_outbox_idempotency(
        db,
        context,
        work=_do_reinforce,
        audit_event=audit_event,
        outbox_event=outbox_event,
        resolve_existing=_resolve_existing,
    )


def get_decay_status(
    db: Session,
    *,
    project_id: UUID | None = None,
) -> dict[str, Any]:
    """Get decay state summary for a project (or globally).

    Returns counts by decay_state and average decay_score.
    """
    rows = db.execute(_GET_DECAY_STATUS, {"pid": project_id}).all()

    status = {
        "project_id": str(project_id) if project_id else None,
        "total_active": 0,
        "total_decaying": 0,
        "total_silent": 0,
        "total_archived": 0,
        "avg_decay_score": 1.0,
    }

    total_count = 0
    total_weighted_score = 0.0

    for row in rows:
        row_data = dict(row._mapping)
        state = str(row_data["decay_state"])
        count = int(row_data["cnt"])
        avg = float(row_data["avg_score"]) if row_data["avg_score"] is not None else 0.0

        key_map = {
            "active": "total_active",
            "decaying": "total_decaying",
            "silent": "total_silent",
            "archived": "total_archived",
        }
        if state in key_map:
            status[key_map[state]] = count

        total_count += count
        total_weighted_score += avg * count

    if total_count > 0:
        status["avg_decay_score"] = round(total_weighted_score / total_count, 4)

    return status
