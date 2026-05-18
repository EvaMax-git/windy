"""Retry Sweeper – scans failed deliveries and retries with exponential backoff.

P2-02: The retry sweeper periodically scans the ``event_deliveries`` table for
rows with ``delivery_state = 'failed'`` that have not exhausted their
``max_attempts``.  Eligible rows are re-queued for dispatch after an
exponential backoff delay.

When ``max_attempts`` is exhausted the delivery is promoted to the
``dead_letters`` table (Dead Letter Queue).

A companion :class:`DispatchingRecoverySweeper` handles deliveries stuck in
``'dispatching'`` state (e.g. after a worker crash).

Failure Classification
----------------------
Errors are classified into one of five ``failure_class`` values (aligning
with the DDL CHECK constraint on ``dead_letters.failure_class``):

* ``provider_transient_exhausted`` – transient provider errors
  (timeout, connection, DNS, 5xx, 429)
* ``policy_denied_terminal`` – policy/permission denials
* ``payload_invalid`` – schema/validation/payload errors
* ``code_bug`` – internal code defects (NPE, assertion, etc.)
* ``external_side_effect_unknown`` – external side effect state unknown

P2-03 integration
-----------------
When a delivery is promoted to ``dead_letters`` the row is created with
``replay_state = 'pending'`` and ``review_required = true``, ready for
the DLQ replay workflow (P2-04).

Configuration
-------------
All tunables come from :class:`mneme.config.Settings`:

* ``worker_retry_base_delay_seconds`` (default 5)
* ``worker_retry_max_delay_seconds`` (default 3600)
* ``worker_retry_max_attempts`` (default 5)
* ``worker_retry_sweeper_interval_seconds`` (default 10)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from mneme.config import get_settings
from mneme.db.base import SessionLocal

logger = logging.getLogger(__name__)


# ── Failure Classification ───────────────────────────────────────────────────

# Keyword-based classifier that maps error message text to one of the five
# ``failure_class`` values defined by the ``dead_letters`` DDL CHECK constraint.

def classify_failure(error_message: str | None) -> str:
    """Classify an error message into a ``failure_class`` category.

    Returns one of:
    ``provider_transient_exhausted`` | ``policy_denied_terminal`` |
    ``payload_invalid`` | ``code_bug`` | ``external_side_effect_unknown``
    """
    if not error_message:
        return "code_bug"

    msg_lower = error_message.lower()

    # Provider transient errors (timeout, connection, DNS, 5xx, 429, rate limit)
    if any(kw in msg_lower for kw in [
        "timeout", "timed out", "connection", "connect",
        "dns", "name resolution", "5xx", "502", "503", "504",
        "503 service unavailable", "too many requests", "429",
        "rate limit", "throttle", "circuit breaker",
        "network", "socket", "eof", "reset by peer",
    ]):
        return "provider_transient_exhausted"

    # Policy / permission denials
    if any(kw in msg_lower for kw in [
        "policy", "permission denied", "access denied", "forbidden",
        "unauthorized", "not allowed", "denied by policy",
        "review required", "step-up", "quota exceeded",
    ]):
        return "policy_denied_terminal"

    # Payload / schema validation
    if any(kw in msg_lower for kw in [
        "validation", "invalid", "schema", "payload",
        "malformed", "bad request", "400", "parse error",
        "type error", "value error", "constraint",
    ]):
        return "payload_invalid"

    # External side effect unknown
    if any(kw in msg_lower for kw in [
        "unknown", "side effect", "external", "idempotency",
        "duplicate", "already exists", "conflict", "409",
    ]):
        return "external_side_effect_unknown"

    # Default: internal code defect
    return "code_bug"


# ── SQL Queries ──────────────────────────────────────────────────────────────

_FIND_FAILED_DELIVERIES = text("""
    SELECT
        ed.delivery_id,
        ed.event_id,
        ed.consumer_name,
        ed.dispatch_attempts,
        ed.last_error,
        ed.failed_at,
        ed.last_dispatched_at,
        e.event_type,
        e.aggregate_type,
        e.aggregate_id,
        e.payload_json
    FROM event_deliveries ed
    JOIN events e ON ed.event_id = e.event_id
    WHERE ed.delivery_state = 'failed'
      AND ed.dispatch_attempts < :max_attempts
    ORDER BY ed.failed_at ASC
    LIMIT :limit
""")

_FIND_EXHAUSTED_DELIVERIES = text("""
    SELECT
        ed.delivery_id,
        ed.event_id,
        ed.consumer_name,
        ed.dispatch_attempts,
        ed.last_error,
        ed.failed_at,
        ed.last_dispatched_at,
        e.event_type,
        e.aggregate_type,
        e.aggregate_id,
        e.payload_json
    FROM event_deliveries ed
    JOIN events e ON ed.event_id = e.event_id
    WHERE ed.delivery_state = 'failed'
      AND ed.dispatch_attempts >= :max_attempts
    ORDER BY ed.failed_at ASC
    LIMIT :limit
""")

_RETRY_DELIVERY = text("""
    UPDATE event_deliveries
    SET delivery_state = 'pending',
        updated_at = now()
    WHERE delivery_id = :delivery_id
""")

_RESET_EVENT_TO_PENDING = text("""
    UPDATE events
    SET publish_state = 'pending'
    WHERE event_id = :event_id
""")

_PROMOTE_TO_DEAD_LETTER = text("""
    UPDATE event_deliveries
    SET delivery_state = 'dead_letter',
        updated_at = now()
    WHERE delivery_id = :delivery_id
""")

_INSERT_DEAD_LETTER = text("""
    INSERT INTO dead_letters (
        source_type,
        source_id,
        related_event_id,
        aggregate_type,
        aggregate_id,
        failure_class,
        error_code,
        error_message,
        retry_exhausted,
        external_effect_state,
        replay_state,
        review_required,
        payload_json,
        first_failed_at,
        last_failed_at
    ) VALUES (
        :source_type,
        :source_id,
        :related_event_id,
        :aggregate_type,
        :aggregate_id,
        :failure_class,
        :error_code,
        :error_message,
        :retry_exhausted,
        :external_effect_state,
        :replay_state,
        :review_required,
        :payload_json,
        :first_failed_at,
        :last_failed_at
    )
""")

_MARK_EVENT_DEAD_LETTER = text("""
    UPDATE events
    SET publish_state = 'dead_letter',
        last_error = :last_error
    WHERE event_id = :event_id
""")


# ── Data Classes ─────────────────────────────────────────────────────────────


@dataclass
class FailedDelivery:
    """A single failed delivery row joined with its parent event."""

    delivery_id: UUID
    event_id: UUID
    consumer_name: str
    dispatch_attempts: int
    last_error: str | None
    failed_at: datetime | None
    last_dispatched_at: datetime | None
    event_type: str
    aggregate_type: str
    aggregate_id: UUID
    payload_json: dict[str, Any]


# ── RetrySweeper ─────────────────────────────────────────────────────────────


class RetrySweeper:
    """Periodic scanner that retries failed deliveries with exponential backoff.

    The sweeper is designed to run in the same worker process as the main
    dispatch loop.  It should only be invoked when the worker holds the
    dispatch lease.

    Each sweep cycle performs two phases:

    1. **Retry eligible** – failed deliveries whose backoff delay has passed
       are reset to ``'pending'`` (on both ``event_deliveries`` and
       ``events``) so the normal poll→dispatch loop picks them up.
    2. **Promote exhausted** – failed deliveries that have reached
       ``max_attempts`` are moved to ``dead_letters`` and marked
       ``delivery_state = 'dead_letter'``.

    Usage::

        sweeper = RetrySweeper(
            base_delay_seconds=5,
            max_delay_seconds=3600,
            max_attempts=5,
        )
        result = sweeper.sweep()
        # result == {"retried": 3, "dead_lettered": 1, "errors": 0}
    """

    def __init__(
        self,
        *,
        base_delay_seconds: int = 5,
        max_delay_seconds: int = 3600,
        max_attempts: int = 5,
        batch_size: int = 50,
    ) -> None:
        self._base_delay = base_delay_seconds
        self._max_delay = max_delay_seconds
        self._max_attempts = max_attempts
        self._batch_size = batch_size

    # ── Public API ───────────────────────────────────────────────────────────

    def sweep(self) -> dict[str, int]:
        """Execute one full sweep cycle.

        Returns
        -------
        dict
            Keys ``"retried"``, ``"dead_lettered"``, ``"errors"`` with counts.
        """
        result: dict[str, int] = {"retried": 0, "dead_lettered": 0, "errors": 0}

        try:
            with SessionLocal() as db:
                # ── Phase 1: retry eligible failed deliveries ────────────────
                retried = self._retry_eligible(db)
                result["retried"] = retried

                # ── Phase 2: promote exhausted to dead_letters ───────────────
                dl_count = self._promote_exhausted(db)
                result["dead_lettered"] = dl_count

                db.commit()

        except Exception as exc:
            logger.error("retry sweeper cycle failed: %s", exc)
            result["errors"] += 1

        return result

    # ── Backoff Calculation ──────────────────────────────────────────────────

    def _compute_backoff(self, attempt: int) -> float:
        """Compute exponential backoff delay for attempt *N*.

        Formula: ``delay = base_delay * 2^(attempt-1)``, capped at ``max_delay``.

        *attempt* is the *current* ``dispatch_attempts`` value, so the
        **next** attempt number is ``attempt + 1``.
        """
        import math
        delay = self._base_delay * (2 ** max(0, attempt - 1))
        return min(float(delay), float(self._max_delay))

    # ── Phase 1: Retry Eligible ──────────────────────────────────────────────

    def _retry_eligible(self, db: Session) -> int:
        """Find and re-queue eligible failed deliveries.

        A delivery is eligible when its backoff delay has elapsed since
        ``failed_at`` and ``dispatch_attempts < max_attempts``.
        """
        rows = (
            db.execute(
                _FIND_FAILED_DELIVERIES,
                {"max_attempts": self._max_attempts, "limit": self._batch_size},
            )
            .mappings()
            .all()
        )

        if not rows:
            return 0

        now = datetime.now(timezone.utc)
        retried = 0

        for row in rows:
            delivery = FailedDelivery(
                delivery_id=row["delivery_id"],
                event_id=row["event_id"],
                consumer_name=row["consumer_name"],
                dispatch_attempts=row["dispatch_attempts"],
                last_error=row["last_error"],
                failed_at=row["failed_at"],
                last_dispatched_at=row["last_dispatched_at"],
                event_type=row["event_type"],
                aggregate_type=row["aggregate_type"],
                aggregate_id=row["aggregate_id"],
                payload_json=(
                    row["payload_json"]
                    if isinstance(row["payload_json"], dict)
                    else {}
                ),
            )

            # ── Backoff check ────────────────────────────────────────────────
            backoff_delay = self._compute_backoff(delivery.dispatch_attempts)

            if delivery.failed_at is not None:
                failed_at_dt = _ensure_datetime(delivery.failed_at)
                elapsed = (now - failed_at_dt).total_seconds()
            else:
                elapsed = float(backoff_delay)  # force eligible

            if elapsed < backoff_delay:
                logger.debug(
                    "retry not yet due – delivery_id=%s attempt=%s/%s "
                    "backoff_s=%.1f elapsed_s=%.1f",
                    delivery.delivery_id,
                    delivery.dispatch_attempts,
                    self._max_attempts,
                    backoff_delay,
                    elapsed,
                )
                continue

            # ── Re-queue ─────────────────────────────────────────────────────
            # Reset delivery to pending so the dispatcher's UPSERT picks it up
            db.execute(
                _RETRY_DELIVERY,
                {"delivery_id": delivery.delivery_id},
            )

            # Reset the parent event to pending so the poller claims it again
            db.execute(
                _RESET_EVENT_TO_PENDING,
                {"event_id": delivery.event_id},
            )

            logger.info(
                "retry scheduled – delivery_id=%s event_id=%s "
                "next_attempt=%s/%s consumer=%s backoff_s=%.1f",
                delivery.delivery_id,
                delivery.event_id,
                delivery.dispatch_attempts + 1,
                self._max_attempts,
                delivery.consumer_name,
                backoff_delay,
            )
            retried += 1

        return retried

    # ── Phase 2: Promote Exhausted ───────────────────────────────────────────

    def _promote_exhausted(self, db: Session) -> int:
        """Promote deliveries that have exhausted ``max_attempts`` to the DLQ."""
        rows = (
            db.execute(
                _FIND_EXHAUSTED_DELIVERIES,
                {"max_attempts": self._max_attempts, "limit": self._batch_size},
            )
            .mappings()
            .all()
        )

        if not rows:
            return 0

        dl_count = 0
        for row in rows:
            delivery_id = row["delivery_id"]
            event_id = row["event_id"]
            last_error = row["last_error"] or "max attempts exhausted"
            raw_failed_at = row["failed_at"]
            first_failed_at = _ensure_datetime(raw_failed_at) if raw_failed_at else datetime.now(timezone.utc)
            last_failed_at = _ensure_datetime(raw_failed_at) if raw_failed_at else datetime.now(timezone.utc)
            payload_dict = (
                row["payload_json"]
                if isinstance(row["payload_json"], dict)
                else {}
            )
            # Serialize to JSON string for DB portability (SQLite uses TEXT)
            payload_str = json.dumps(payload_dict) if payload_dict else "{}"

            # Classify the failure
            failure_class = classify_failure(last_error)

            # Truncate error message to avoid oversized rows (dead_letters.error_message is text,
            # but keeping it reasonable)
            error_message = (last_error or "max attempts exhausted")[:5000]

            # ── Insert dead_letter row ───────────────────────────────────────
            db.execute(
                _INSERT_DEAD_LETTER,
                {
                    "source_type": "event_delivery",
                    "source_id": delivery_id,
                    "related_event_id": event_id,
                    "aggregate_type": row["aggregate_type"],
                    "aggregate_id": row["aggregate_id"],
                    "failure_class": failure_class,
                    "error_code": None,
                    "error_message": error_message,
                    "retry_exhausted": True,
                    "external_effect_state": "none",
                    "replay_state": "pending",
                    "review_required": True,
                    "payload_json": payload_str,
                    "first_failed_at": first_failed_at,
                    "last_failed_at": last_failed_at,
                },
            )

            # ── Mark delivery as dead_letter ─────────────────────────────────
            db.execute(
                _PROMOTE_TO_DEAD_LETTER,
                {"delivery_id": delivery_id},
            )

            # ── Mark event as dead_letter ────────────────────────────────────
            db.execute(
                _MARK_EVENT_DEAD_LETTER,
                {
                    "event_id": event_id,
                    "last_error": error_message[:2000],
                },
            )

            logger.warning(
                "delivery promoted to dead_letter – delivery_id=%s event_id=%s "
                "attempts=%s/%s consumer=%s failure_class=%s",
                delivery_id,
                event_id,
                row["dispatch_attempts"],
                self._max_attempts,
                row["consumer_name"],
                failure_class,
            )
            dl_count += 1

        return dl_count


# ── Internal helpers ──────────────────────────────────────────────────────────


def _ensure_datetime(value: Any) -> datetime:
    """Coerce *value* to a timezone-aware ``datetime``.

    Handles both Python ``datetime`` objects (PostgreSQL) and ISO-8601
    strings (SQLite).
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            # Fallback: try parsing common formats
            from datetime import datetime as Datetime
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
                        "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
                try:
                    dt = Datetime.strptime(value, fmt)
                    break
                except ValueError:
                    continue
            else:
                return datetime.now(timezone.utc)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt
    return datetime.now(timezone.utc)


# ── Convenience Factory ──────────────────────────────────────────────────────


def create_retry_sweeper() -> RetrySweeper:
    """Create a :class:`RetrySweeper` from application settings."""
    settings = get_settings()
    return RetrySweeper(
        base_delay_seconds=settings.worker_retry_base_delay_seconds,
        max_delay_seconds=settings.worker_retry_max_delay_seconds,
        max_attempts=settings.worker_retry_max_attempts,
    )
