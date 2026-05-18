"""P6-10 Spontaneous Recall Sweeper — idle background scanner that detects memory
contradictions and proactively notifies users.

This sweeper is the **autonomous insight** component. Unlike the on-demand
``run_conflict_pipeline``, which runs when explicitly invoked, the spontaneous
recall sweeper:

1. Periodically scans **all active memories** across all projects during idle
   periods.
2. Uses embedding similarity to find memory pairs in the "conflict zone".
3. Uses LLM to evaluate whether contradictions exist.
4. When conflicts are confirmed, **actively creates inbox notifications** and
   review items so users are alerted without manual intervention.

Architecture
------------
* Runs inside the worker main loop (same pattern as RetrySweeper / DecaySweeper).
* Each cycle:
  1. Fetches active memories with ready embeddings.
  2. Computes pairwise cosine similarity; keeps pairs in the conflict zone
     (``threshold_low <= sim < threshold_high``).
  3. Sends candidate pairs to LLM for semantic conflict evaluation.
  4. Confirmed conflicts (``confidence >= min_confidence``) trigger:
     - ``conflicts_with`` memory_relations
     - ``review_items`` for human review
     - ``inbox_items`` as active user notifications
  5. Returns a structured result summary.

Configuration
-------------
All tunables from ``mneme.config.Settings``:

* ``worker_spontaneous_recall_enabled`` (default ``True``)
* ``worker_spontaneous_recall_interval_seconds`` (default 300 = 5 min)
* ``worker_spontaneous_recall_min_confidence`` (default 0.65)
* ``worker_spontaneous_recall_max_pairs`` (default 20)
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

from mneme.api.context import ActorContext, RequestContext
from mneme.config import get_settings
from mneme.db.base import SessionLocal
from mneme.gateway.call import Gateway, GatewayError
from mneme.memory.refine.conflict import (
    ConflictCandidate,
    ConflictResult,
    _CONFLICT_SYSTEM,
    _parse_llm_conflict_response,
)

logger = logging.getLogger(__name__)

_SYSTEM_USER_ID = UUID("00000000-0000-0000-0000-000000000000")

# ── 冲突检测LLM Prompt ──────────────────────────────────────────────────────────

_CONFLICT_SYSTEM_ZH = (
    "你是记忆矛盾检测器。给定两条记忆陈述，判断它们是否包含相互矛盾的事实、决策或约束。\n\n"
    "仅返回JSON：\n"
    '{"conflict": true/false, "reason": "<中文解释>", "confidence": 0.0-1.0}'
)


# ── Data types ──────────────────────────────────────────────────────────────────


@dataclass
class SpontaneousRecallResult:
    """Aggregated result from a spontaneous recall sweep cycle."""

    memories_scanned: int = 0
    """Total active memories with embeddings found."""

    pairs_evaluated: int = 0
    """Number of conflict-zone pairs sent to LLM."""

    conflicts_confirmed: int = 0
    """Number of LLM-confirmed contradictions."""

    relations_created: int = 0
    """Number of ``conflicts_with`` relations written."""

    inbox_notifications: int = 0
    """Number of inbox notifications created."""

    review_items_created: int = 0
    """Number of review items created."""

    errors: int = 0
    """Number of errors encountered during this sweep."""

    details: list[dict[str, Any]] = field(default_factory=list)
    """Per-conflict detail records."""


# ── SQL Queries ─────────────────────────────────────────────────────────────────

_READY_EMBEDDINGS = text("""
    SELECT DISTINCT ON (mie.memory_id)
        mie.memory_index_entry_id,
        mie.memory_id,
        mie.embedding,
        m.title,
        m.memory_text,
        m.canonical_key,
        m.project_id
    FROM memory_index_entries mie
    JOIN memories m ON m.memory_id = mie.memory_id
    WHERE mie.vector_state = 'ready'
      AND mie.embedding IS NOT NULL
      AND m.status = 'active'
    ORDER BY mie.memory_id, mie.memory_version DESC
    LIMIT :limit
""")

_LAST_SWEEP_TIME = text("""
    SELECT MAX(last_sweep_at)
    FROM spontaneous_recall_sweeps
""")

_RECORD_SWEEP = text("""
    INSERT INTO spontaneous_recall_sweeps (
        sweep_id,
        sweep_at,
        memories_scanned,
        pairs_evaluated,
        conflicts_confirmed,
        relations_created,
        inbox_notifications,
        review_items_created,
        errors
    ) VALUES (
        :sweep_id,
        :sweep_at,
        :memories_scanned,
        :pairs_evaluated,
        :conflicts_confirmed,
        :relations_created,
        :inbox_notifications,
        :review_items_created,
        :errors
    )
""")


# ── Helpers ─────────────────────────────────────────────────────────────────────


def _make_system_context() -> RequestContext:
    """Build a minimal RequestContext for system-initiated actions."""
    return RequestContext(
        request_id=uuid4(),
        correlation_id=uuid4(),
        actor=ActorContext(
            actor_type="system",
            actor_id=_SYSTEM_USER_ID,
        ),
        idempotency_key=None,
    )


def _parse_stored_embedding(value) -> list[float] | None:
    """Parse an embedding value from the DB."""
    import json

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


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    """Compute cosine similarity between two embedding vectors."""
    import math

    size = min(len(left), len(right))
    if size == 0:
        return 0.0
    dot = sum(left[i] * right[i] for i in range(size))
    left_norm = math.sqrt(sum(left[i] * left[i] for i in range(size)))
    right_norm = math.sqrt(sum(right[i] * right[i] for i in range(size)))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


# ── SpontaneousRecallSweeper ────────────────────────────────────────────────────


class SpontaneousRecallSweeper:
    """Periodic sweeper that scans memories for contradictions and notifies users.

    Usage::

        sweeper = create_spontaneous_recall_sweeper()
        result = sweeper.sweep()
        # result.conflicts_confirmed, .inbox_notifications, ...
    """

    def __init__(
        self,
        *,
        min_confidence: float = 0.65,
        max_pairs: int = 20,
        threshold_low: float = 0.70,
        threshold_high: float = 0.92,
        model: str = "deepseek-chat",
    ) -> None:
        self._settings = get_settings()
        self._min_confidence = min_confidence
        self._max_pairs = max_pairs
        self._threshold_low = threshold_low
        self._threshold_high = threshold_high
        self._model = model

    # ── Public API ────────────────────────────────────────────────────────────

    def sweep(self) -> SpontaneousRecallResult:
        """Execute one full spontaneous recall sweep cycle.

        Returns
        -------
        SpontaneousRecallResult
            Counters and detail records for the sweep.
        """
        result = SpontaneousRecallResult()

        try:
            # Phase 1: Fetch active memories with ready embeddings
            entries = self._fetch_active_with_embeddings()
            result.memories_scanned = len(entries)
            logger.info(
                "spontaneous_recall: scanned %d active memories with embeddings",
                len(entries),
            )

            if len(entries) < 2:
                logger.debug("spontaneous_recall: insufficient memories (<2), skipping")
                self._record_sweep(result)
                return result

            # Phase 2: Find conflict-zone pairs
            candidates = self._find_conflict_candidates(entries)
            logger.info(
                "spontaneous_recall: found %d conflict-zone candidates",
                len(candidates),
            )

            if not candidates:
                self._record_sweep(result)
                return result

            # Phase 3: LLM evaluation
            evaluated = self._evaluate_with_llm(candidates)
            result.pairs_evaluated = len(evaluated)
            result.conflicts_confirmed = sum(1 for c in evaluated if c.conflict)
            logger.info(
                "spontaneous_recall: LLM evaluated %d pairs, %d confirmed conflicts",
                len(evaluated),
                result.conflicts_confirmed,
            )

            # Phase 4: Apply confirmed conflicts (create relations + notifications)
            if result.conflicts_confirmed > 0:
                apply_result = self._apply_conflicts(evaluated)
                result.relations_created = apply_result.get("relations_created", 0)
                result.inbox_notifications = apply_result.get("inbox_notifications", 0)
                result.review_items_created = apply_result.get("review_items_created", 0)
                result.details = apply_result.get("details", [])

            # Phase 5: Record sweep
            self._record_sweep(result)

        except Exception as exc:
            logger.error("spontaneous_recall: sweep cycle failed: %s", exc, exc_info=True)
            result.errors += 1

        return result

    # ── Phase 1: Fetch embeddings ─────────────────────────────────────────────

    def _fetch_active_with_embeddings(
        self,
        *,
        limit: int = 2000,
    ) -> list[dict]:
        """Return dicts for active memories with ready embeddings."""
        db = SessionLocal()
        try:
            rows = db.execute(
                _READY_EMBEDDINGS,
                {"limit": limit},
            ).all()

            results: list[dict] = []
            for row in rows:
                data = dict(row._mapping)
                embedding = _parse_stored_embedding(data.get("embedding"))
                if embedding is None:
                    continue
                results.append({
                    "memory_id": data["memory_id"],
                    "embedding": embedding,
                    "title": data.get("title"),
                    "memory_text": data.get("memory_text"),
                    "canonical_key": data.get("canonical_key", ""),
                    "project_id": data.get("project_id"),
                })
            return results
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    # ── Phase 2: Find conflict candidates ─────────────────────────────────────

    def _find_conflict_candidates(
        self,
        entries: list[dict],
    ) -> list[ConflictCandidate]:
        """Find pairs in the conflict zone [threshold_low, threshold_high)."""
        candidates: list[ConflictCandidate] = []
        n = len(entries)

        for i in range(n):
            for j in range(i + 1, n):
                a, b = entries[i], entries[j]
                sim = _cosine_similarity(a["embedding"], b["embedding"])
                if self._threshold_low <= sim < self._threshold_high:
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

        # Sort by similarity descending (most similar → most likely paradox)
        candidates.sort(key=lambda c: c.similarity, reverse=True)
        return candidates[:self._max_pairs]

    # ── Phase 3: LLM evaluation ──────────────────────────────────────────────

    def _evaluate_with_llm(
        self,
        candidates: list[ConflictCandidate],
    ) -> list[ConflictCandidate]:
        """Use LLM to evaluate whether candidate pairs are actual contradictions."""
        if not candidates:
            return candidates

        try:
            from mneme.gateway.call import Gateway
            gateway = Gateway()
        except Exception as exc:
            logger.warning(
                "spontaneous_recall: cannot create Gateway (%s), skipping LLM eval",
                exc,
            )
            return candidates

        evaluated = 0
        for candidate in candidates:
            messages = [
                {"role": "system", "content": _CONFLICT_SYSTEM_ZH},
                {
                    "role": "user",
                    "content": (
                        f"记忆A: {candidate.memory_a_text or ''}\n\n"
                        f"记忆B: {candidate.memory_b_text or ''}"
                    ),
                },
            ]
            try:
                result = gateway.call(
                    capability_code="chat.completion",
                    params={
                        "model": self._model,
                        "messages": messages,
                        "temperature": 0.1,
                        "max_tokens": 512,
                    },
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
                    "spontaneous_recall: LLM eval failed for %s<->%s: %s",
                    candidate.memory_a_id,
                    candidate.memory_b_id,
                    exc,
                )

        logger.info(
            "spontaneous_recall: LLM evaluated %d/%d pairs",
            evaluated,
            len(candidates),
        )
        return candidates

    # ── Phase 4: Apply confirmed conflicts ────────────────────────────────────

    def _apply_conflicts(
        self,
        candidates: list[ConflictCandidate],
    ) -> dict[str, Any]:
        """Create relations, review items, and inbox notifications for confirmed conflicts."""
        confirmed = [
            c for c in candidates
            if c.conflict and c.confidence >= self._min_confidence
        ]

        if not confirmed:
            return {
                "relations_created": 0,
                "inbox_notifications": 0,
                "review_items_created": 0,
                "details": [],
            }

        result = {
            "relations_created": 0,
            "inbox_notifications": 0,
            "review_items_created": 0,
            "details": [],
        }

        for candidate in confirmed:
            detail = {
                "memory_a_id": str(candidate.memory_a_id),
                "memory_b_id": str(candidate.memory_b_id),
                "similarity": candidate.similarity,
                "confidence": candidate.confidence,
                "reason": candidate.reason,
            }

            try:
                db = SessionLocal()
                context = _make_system_context()

                # Create conflicts_with relation
                relation = self._create_conflict_relation(
                    db, context, candidate,
                )
                if relation:
                    result["relations_created"] += 1
                    detail["relation_id"] = str(relation.get("memory_relation_id", ""))

                # Create review item
                review_item = self._create_review_item_for_conflict(
                    db, context, candidate,
                )
                if review_item:
                    result["review_items_created"] += 1
                    detail["review_item_id"] = str(review_item.get("review_item_id", ""))

                # Create inbox notification
                inbox_item = self._create_inbox_notification(
                    db, context, candidate,
                )
                if inbox_item:
                    result["inbox_notifications"] += 1
                    detail["inbox_item_id"] = str(inbox_item.get("inbox_item_id", ""))

                db.commit()
                result["details"].append(detail)

                logger.info(
                    "spontaneous_recall: created alert for conflict %s<->%s "
                    "(similarity=%.2f, confidence=%.2f)",
                    candidate.memory_a_id,
                    candidate.memory_b_id,
                    candidate.similarity,
                    candidate.confidence,
                )

            except Exception as exc:
                logger.error(
                    "spontaneous_recall: failed to apply conflict %s<->%s: %s",
                    candidate.memory_a_id,
                    candidate.memory_b_id,
                    exc,
                    exc_info=True,
                )
                try:
                    db.rollback()
                except Exception:
                    pass
            finally:
                try:
                    db.close()
                except Exception:
                    pass

        return result

    def _create_conflict_relation(
        self,
        db,
        context: RequestContext,
        candidate: ConflictCandidate,
    ) -> dict | None:
        """Create a ``conflicts_with`` memory_relation."""
        try:
            from mneme.db.memory_relations import create_memory_relation
            from mneme.schemas.memory_relations import MemoryRelationCreate, RelationType

            payload = MemoryRelationCreate(
                from_memory_id=candidate.memory_a_id,
                to_memory_id=candidate.memory_b_id,
                relation_type=RelationType.conflicts_with,
                reason=(
                    candidate.reason
                    or f"Auto-detected contradiction (sim={candidate.similarity:.2f}, "
                       f"confidence={candidate.confidence:.2f})"
                ),
                metadata_json={
                    "similarity": candidate.similarity,
                    "confidence": candidate.confidence,
                    "source": "spontaneous_recall_sweeper",
                    "llm_reason": candidate.reason or "",
                    "memory_a_title": candidate.memory_a_title or "",
                    "memory_b_title": candidate.memory_b_title or "",
                },
            )

            relation = create_memory_relation(db, context, payload=payload)
            return {
                "memory_relation_id": relation.memory_relation_id,
            }
        except ValueError as exc:
            logger.debug(
                "spontaneous_recall: relation already exists: %s", exc,
            )
            return None
        except Exception as exc:
            logger.warning(
                "spontaneous_recall: failed to create conflict relation: %s", exc,
            )
            return None

    def _create_review_item_for_conflict(
        self,
        db,
        context: RequestContext,
        candidate: ConflictCandidate,
    ) -> dict | None:
        """Create a review_item for a detected contradiction."""
        try:
            from mneme.db.memories import get_memory
            from mneme.db.review_items import create_review_item

            mem_a = get_memory(db, candidate.memory_a_id)
            if mem_a is None:
                return None

            project_id = mem_a.project_id or UUID("00000000-0000-0000-0000-000000000000")

            review_item = create_review_item(
                project_id=project_id,
                review_type="conflict_resolution",
                target_type="memory_relation",
                target_id=candidate.memory_a_id,  # will be updated after relation creation
                priority=75,
                requester_actor_type="system",
                requester_actor_id=_SYSTEM_USER_ID,
                decision_payload={
                    "similarity": candidate.similarity,
                    "confidence": candidate.confidence,
                    "llm_reason": candidate.reason or "",
                    "memory_a_id": str(candidate.memory_a_id),
                    "memory_b_id": str(candidate.memory_b_id),
                    "memory_a_title": candidate.memory_a_title or "",
                    "memory_b_title": candidate.memory_b_title or "",
                    "source": "spontaneous_recall_sweeper",
                },
                correlation_id=context.correlation_id,
                request_id=context.request_id,
                idempotency_key=f"sr-review-{candidate.memory_a_id}-{candidate.memory_b_id}",
            )
            return review_item
        except Exception as exc:
            logger.warning(
                "spontaneous_recall: failed to create review_item: %s", exc,
            )
            return None

    def _create_inbox_notification(
        self,
        db,
        context: RequestContext,
        candidate: ConflictCandidate,
    ) -> dict | None:
        """Create an inbox notification alerting the user to a detected contradiction."""
        try:
            from mneme.db.memories import get_memory
            from mneme.db.inbox import create_inbox_item
            from mneme.schemas.storage import InboxItemCreateRequest

            mem_a = get_memory(db, candidate.memory_a_id)
            project_id = mem_a.project_id if mem_a else None

            if project_id is None:
                logger.warning(
                    "spontaneous_recall: cannot create inbox notification — "
                    "no project_id for memory %s",
                    candidate.memory_a_id,
                )
                return None

            title = (
                f"⚠️ 记忆矛盾提醒: "
                f"\"{candidate.memory_a_title or candidate.canonical_key_a or '未知'}\" ↔ "
                f"\"{candidate.memory_b_title or candidate.canonical_key_b or '未知'}\""
            )

            # Generate idempotency key
            raw = f"sr-inbox-{candidate.memory_a_id}-{candidate.memory_b_id}"
            ikey = hashlib.sha256(raw.encode()).hexdigest()

            payload = InboxItemCreateRequest(
                project_id=project_id,
                inbox_type="alert",
                source="spontaneous_recall",
                source_uri=None,
                source_ref=f"memory:{candidate.memory_a_id},{candidate.memory_b_id}",
                title=title[:200],
                content_hash=None,
                payload_json={
                    "alert_type": "memory_contradiction",
                    "memory_a_id": str(candidate.memory_a_id),
                    "memory_b_id": str(candidate.memory_b_id),
                    "memory_a_title": candidate.memory_a_title or "",
                    "memory_b_title": candidate.memory_b_title or "",
                    "similarity": candidate.similarity,
                    "confidence": candidate.confidence,
                    "llm_reason": candidate.reason or "",
                    "suggested_action": "请审查这两条记忆是否存在真实矛盾，决定保留、合并、或标记为已解决。",
                },
                metadata_json={
                    "source": "spontaneous_recall_sweeper",
                    "canonical_key_a": candidate.canonical_key_a or "",
                    "canonical_key_b": candidate.canonical_key_b or "",
                },
            )

            context_with_ikey = RequestContext(
                request_id=uuid4(),
                correlation_id=uuid4(),
                actor=ActorContext(
                    actor_type="system",
                    actor_id=_SYSTEM_USER_ID,
                ),
                idempotency_key=ikey,
            )

            inbox_item = create_inbox_item(
                db, context_with_ikey, payload=payload, status="received",
            )
            return {"inbox_item_id": str(inbox_item.inbox_item_id)}
        except Exception as exc:
            logger.warning(
                "spontaneous_recall: failed to create inbox notification: %s", exc,
            )
            return None

    # ── Phase 5: Record sweep ─────────────────────────────────────────────────

    def _record_sweep(self, result: SpontaneousRecallResult) -> None:
        """Record sweep results to the sweep tracking table (if exists)."""
        try:
            db = SessionLocal()
            db.execute(
                _RECORD_SWEEP,
                {
                    "sweep_id": uuid4(),
                    "sweep_at": datetime.now(timezone.utc),
                    "memories_scanned": result.memories_scanned,
                    "pairs_evaluated": result.pairs_evaluated,
                    "conflicts_confirmed": result.conflicts_confirmed,
                    "relations_created": result.relations_created,
                    "inbox_notifications": result.inbox_notifications,
                    "review_items_created": result.review_items_created,
                    "errors": result.errors,
                },
            )
            db.commit()
        except Exception:
            # Table may not exist yet — non-fatal
            try:
                db.rollback()
            except Exception:
                pass
            logger.debug(
                "spontaneous_recall: could not record sweep (table may not exist)",
            )
        finally:
            try:
                db.close()
            except Exception:
                pass


# ── Convenience Factory ─────────────────────────────────────────────────────────


def create_spontaneous_recall_sweeper() -> SpontaneousRecallSweeper:
    """Create a :class:`SpontaneousRecallSweeper` from application settings."""
    settings = get_settings()
    return SpontaneousRecallSweeper(
        min_confidence=settings.worker_spontaneous_recall_min_confidence,
        max_pairs=settings.worker_spontaneous_recall_max_pairs,
    )
