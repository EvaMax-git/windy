"""P2-07 Review Timeout Checker — expired review detection and alerting placeholder.

This module provides a lightweight sweeper that periodically scans the
``review_items`` table for items whose ``expires_at`` timestamp has passed
while they are still in ``pending`` or ``in_review`` status.

For Phase 2 this is a **placeholder**: it detects expired reviews and
marks them as ``expired`` with an outbox event ``review.expired``, but
does **not** implement a full escalation pipeline (email, webhook, paging).

Phase 3+ can extend this with:
* Notification dispatch (email, Slack, webhook)
* Escalation policies (reminders at T-24h, T-1h)
* Configurable per-review_type expiry windows

Usage
-----
Called periodically by the worker (alongside retry/recovery sweepers)::

    from mneme.worker.review_timeout_checker import check_expired_reviews

    result = check_expired_reviews()
    # result: {"expired": N, "errors": 0}
"""

from __future__ import annotations

import logging
from uuid import UUID, uuid4

from sqlalchemy import text

from mneme.db.base import SessionLocal

logger = logging.getLogger(__name__)

# ── SQL templates ───────────────────────────────────────────────────────────

_EXPIRE_ELIGIBLE_REVIEWS = text("""
    UPDATE review_items
    SET status = 'expired',
        decision = 'expired',
        decided_at = :now_ts,
        updated_at = :now_ts
    WHERE status IN ('pending', 'in_review')
      AND expires_at IS NOT NULL
      AND expires_at <= :now_ts
    RETURNING review_item_id, review_type, target_type, target_id,
              idempotency_key, correlation_id, request_id
""")

_INSERT_EXPIRED_OUTBOX_EVENT = text("""
    INSERT INTO events (
        event_id,
        event_type,
        aggregate_type,
        aggregate_id,
        aggregate_version,
        correlation_id,
        causation_id,
        idempotency_key,
        producer,
        payload_json,
        visibility,
        publish_state,
        occurred_at
    )
    VALUES (
        :event_id,
        :event_type,
        :aggregate_type,
        :aggregate_id,
        :aggregate_version,
        :correlation_id,
        :causation_id,
        :idempotency_key,
        :producer,
        :payload_json,
        :visibility,
        :publish_state,
        :occurred_at
    )
    ON CONFLICT (idempotency_key) DO NOTHING
""")


def check_expired_reviews() -> dict[str, int]:
    """Scan for and expire review items past their ``expires_at`` timestamp.

    For each expired review item:
    1. Transition ``status`` to ``'expired'`` and set ``decision = 'expired'``.
    2. Publish an outbox event ``review.expired`` so the
       :class:`~mneme.worker.consumers.review_consumer.ReviewEventConsumer`
       can log / handle it.

    Returns
    -------
    dict
        ``{"expired": N, "errors": E}`` where *N* is the number of reviews
        expired this cycle, and *E* is the count of outbox-write failures.
    """
    from datetime import datetime, timezone

    now_ts = datetime.now(timezone.utc)
    expired_count = 0
    error_count = 0

    with SessionLocal() as db:
        try:
            rows = (
                db.execute(_EXPIRE_ELIGIBLE_REVIEWS, {"now_ts": now_ts})
                .mappings()
                .all()
            )

            if not rows:
                return {"expired": 0, "errors": 0}

            for row in rows:
                review_item_id = row["review_item_id"]
                review_type = row["review_type"]
                target_type = row["target_type"]
                target_id = row["target_id"]
                idempotency_key = row["idempotency_key"]
                correlation_id = row.get("correlation_id", uuid4())
                request_id = row.get("request_id", uuid4())

                # Publish outbox event for review.expired
                # Use idempotency key to prevent duplicates
                outbox_key = f"{idempotency_key}.expired"

                try:
                    db.execute(
                        _INSERT_EXPIRED_OUTBOX_EVENT,
                        {
                            "event_id": uuid4(),
                            "event_type": "review.expired",
                            "aggregate_type": "review_item",
                            "aggregate_id": review_item_id,
                            "aggregate_version": 1,
                            "correlation_id": correlation_id,
                            "causation_id": None,
                            "idempotency_key": outbox_key,
                            "producer": "mneme-worker",
                            "payload_json": {
                                "review_type": review_type,
                                "target_type": target_type,
                                "target_id": str(target_id),
                                "expired_by": "timeout_checker",
                            },
                            "visibility": "internal",
                            "publish_state": "pending",
                            "occurred_at": now_ts,
                        },
                    )
                    expired_count += 1
                    logger.warning(
                        "review expired – review_item=%s type=%s target=%s/%s",
                        review_item_id,
                        review_type,
                        target_type,
                        target_id,
                    )
                except Exception as exc:
                    error_count += 1
                    logger.error(
                        "review expired – failed to write outbox event for "
                        "review_item=%s: %s",
                        review_item_id,
                        exc,
                    )

            db.commit()

        except Exception:
            db.rollback()
            raise

    if expired_count:
        logger.info(
            "review timeout checker – expired=%d errors=%d",
            expired_count,
            error_count,
        )

    return {"expired": expired_count, "errors": error_count}
