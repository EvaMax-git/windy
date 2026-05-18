"""P6-02.4 Memory Expire — rule-based automatic expiration of low-value / stale memories.

Expiration rules (configurable)
-------------------------------
1. ``quality_score < min_quality`` AND created more than ``max_age_days`` ago.
2. ``search_weight < min_weight`` AND no updates in ``stale_days``.
3. Already consumed in a merge (``status='merged'``) — auto-handled.
4. In ``conflicts_with`` relation with 3+ other active memories.

All expire operations call ``mneme.db.memories.expire_memory()`` which
transitions ``active → expired`` through the standard state machine with
full audit + outbox + idempotency.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from mneme.api.context import RequestContext
from mneme.db.memories import MemoryRead, expire_memory, get_memory

logger = logging.getLogger(__name__)

# ── Data types ────────────────────────────────────────────────────────────────


@dataclass
class ExpireRule:
    """A single expiration rule definition."""

    name: str
    """Rule identifier (e.g. ``low_quality_old``)."""

    description: str = ""
    """Human-readable description."""

    enabled: bool = True
    """Whether this rule is active."""


@dataclass
class ExpireCandidate:
    """A memory flagged for expiration with the triggering rule."""

    memory_id: UUID
    canonical_key: str
    title: str | None = None
    status: str = "active"
    reason: str = ""
    rule_name: str = ""
    quality_score: float | None = None
    search_weight: float | None = None


@dataclass
class ExpireScanOutput:
    """Output of ``scan_expire_candidates()``."""

    candidates: list[ExpireCandidate] = field(default_factory=list)
    total_scanned: int = 0
    rules_used: list[str] = field(default_factory=list)


@dataclass
class ExpireApplyOutput:
    """Output of ``apply_expire_batch()``."""

    expired_count: int = 0
    failed_count: int = 0
    errors: list[str] = field(default_factory=list)


# ── Default rules ─────────────────────────────────────────────────────────────

DEFAULT_RULES: list[ExpireRule] = [
    ExpireRule(
        name="low_quality_old",
        description="quality_score < 0.3 and created more than 30 days ago",
    ),
    ExpireRule(
        name="zero_weight_stale",
        description="search_weight < 0.2 and no updates in 90 days",
    ),
    ExpireRule(
        name="high_conflict_count",
        description="in 3+ conflicts_with relations with other active memories",
    ),
    ExpireRule(
        name="merged_consumed",
        description="status='merged' (consumed into another memory)",
    ),
]

# ── SQL queries ───────────────────────────────────────────────────────────────

_LOW_QUALITY_OLD = sql_text("""
    SELECT m.memory_id, m.canonical_key, m.title, m.status,
           m.quality_score, m.search_weight
    FROM memories m
    WHERE (:project_id IS NULL OR m.project_id = :project_id)
      AND m.status = 'active'
      AND m.quality_score IS NOT NULL
      AND m.quality_score < :min_quality
      AND m.created_at < :cutoff_date
    ORDER BY m.quality_score ASC
    LIMIT :limit
""")

_ZERO_WEIGHT_STALE = sql_text("""
    SELECT m.memory_id, m.canonical_key, m.title, m.status,
           m.quality_score, m.search_weight
    FROM memories m
    WHERE (:project_id IS NULL OR m.project_id = :project_id)
      AND m.status = 'active'
      AND m.search_weight < :min_weight
      AND m.updated_at < :cutoff_date
    ORDER BY m.search_weight ASC
    LIMIT :limit
""")

_HIGH_CONFLICT = sql_text("""
    SELECT m.memory_id, m.canonical_key, m.title, m.status,
           m.quality_score, m.search_weight
    FROM memories m
    WHERE (:project_id IS NULL OR m.project_id = :project_id)
      AND m.status = 'active'
      AND (
        SELECT count(*)
        FROM memory_relations mr
        WHERE (mr.from_memory_id = m.memory_id OR mr.to_memory_id = m.memory_id)
          AND mr.relation_type = 'conflicts_with'
      ) >= :min_conflicts
    ORDER BY m.quality_score ASC NULLS LAST
    LIMIT :limit
""")

_MERGED_CONSUMED = sql_text("""
    SELECT m.memory_id, m.canonical_key, m.title, m.status,
           m.quality_score, m.search_weight
    FROM memories m
    WHERE (:project_id IS NULL OR m.project_id = :project_id)
      AND m.status = 'merged'
    ORDER BY m.updated_at ASC
    LIMIT :limit
""")


# ── Rule scanning ─────────────────────────────────────────────────────────────


def scan_expire_candidates(
    db: Session,
    *,
    project_id: UUID | None = None,
    rules: list[ExpireRule] | None = None,
    max_candidates: int = 50,
    min_quality: float = 0.3,
    max_age_days: int = 30,
    min_weight: float = 0.2,
    stale_days: int = 90,
    min_conflicts: int = 3,
) -> ExpireScanOutput:
    """Scan for memory expiration candidates using configured rules.

    Parameters
    ----------
    db : Session
        Active SQLAlchemy session.
    project_id : UUID | None
        Limit scan to one project.  ``None`` = all projects.
    rules : list[ExpireRule] | None
        Rules to evaluate.  Defaults to ``DEFAULT_RULES``.
    max_candidates : int
        Maximum candidates to return per rule.
    min_quality : float
        Quality threshold for ``low_quality_old`` rule.
    max_age_days : int
        Minimum age in days for ``low_quality_old`` rule.
    min_weight : float
        Search weight threshold for ``zero_weight_stale`` rule.
    stale_days : int
        Days without updates for ``zero_weight_stale`` rule.
    min_conflicts : int
        Minimum conflict count for ``high_conflict_count`` rule.

    Returns
    -------
    ExpireScanOutput
    """
    effective_rules = rules or DEFAULT_RULES
    output = ExpireScanOutput()
    now = datetime.now(timezone.utc)
    seen_ids: set[UUID] = set()

    for rule in effective_rules:
        if not rule.enabled:
            continue
        output.rules_used.append(rule.name)
        rows: list[Any] = []

        if rule.name == "low_quality_old":
            cutoff = now - timedelta(days=max_age_days)
            rows = db.execute(
                _LOW_QUALITY_OLD,
                {
                    "project_id": project_id,
                    "min_quality": min_quality,
                    "cutoff_date": cutoff,
                    "limit": max_candidates,
                },
            ).all()

        elif rule.name == "zero_weight_stale":
            cutoff = now - timedelta(days=stale_days)
            rows = db.execute(
                _ZERO_WEIGHT_STALE,
                {
                    "project_id": project_id,
                    "min_weight": min_weight,
                    "cutoff_date": cutoff,
                    "limit": max_candidates,
                },
            ).all()

        elif rule.name == "high_conflict_count":
            rows = db.execute(
                _HIGH_CONFLICT,
                {
                    "project_id": project_id,
                    "min_conflicts": min_conflicts,
                    "limit": max_candidates,
                },
            ).all()

        elif rule.name == "merged_consumed":
            rows = db.execute(
                _MERGED_CONSUMED,
                {
                    "project_id": project_id,
                    "limit": max_candidates,
                },
            ).all()

        for row in rows:
            data = dict(row._mapping)
            mid = data["memory_id"]
            if mid in seen_ids:
                continue
            seen_ids.add(mid)
            output.candidates.append(
                ExpireCandidate(
                    memory_id=mid,
                    canonical_key=data["canonical_key"],
                    title=data.get("title"),
                    status=data["status"],
                    reason=f"Rule '{rule.name}': {rule.description}",
                    rule_name=rule.name,
                    quality_score=data.get("quality_score"),
                    search_weight=data.get("search_weight"),
                )
            )

    output.total_scanned = len(seen_ids)
    return output


# ── Expire application ────────────────────────────────────────────────────────


def apply_expire(
    db: Session,
    context: RequestContext,
    *,
    memory_id: UUID,
    reason: str | None = None,
) -> tuple[bool, str | None]:
    """Expire a single memory through ``db/memories.py:expire_memory()``.

    Skips gracefully if the memory is not in ``active`` or ``merged`` status.

    Returns ``(success, error_message)``.
    """
    mem = get_memory(db, memory_id)
    if mem is None:
        return False, f"Memory {memory_id} not found"
    if mem.status not in ("active", "merged"):
        return False, f"Memory {memory_id} is '{mem.status}', not active/merged"

    try:
        expire_memory(db, context, memory_id=memory_id)
        logger.info("Expired memory %s (%s) — %s", memory_id, mem.canonical_key, reason or "")
        return True, None
    except Exception as exc:
        logger.exception("Failed to expire memory %s", memory_id)
        return False, str(exc)


def apply_expire_batch(
    db: Session,
    context: RequestContext,
    *,
    candidates: list[ExpireCandidate],
) -> ExpireApplyOutput:
    """Expire a batch of candidates from ``scan_expire_candidates()``.

    Returns
    -------
    ExpireApplyOutput
    """
    output = ExpireApplyOutput()
    for candidate in candidates:
        success, error = apply_expire(
            db, context,
            memory_id=candidate.memory_id,
            reason=candidate.reason,
        )
        if success:
            output.expired_count += 1
        else:
            output.failed_count += 1
            if error:
                output.errors.append(error)
    return output
