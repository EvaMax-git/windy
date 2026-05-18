"""Dispatching Recovery Sweeper – recovers events stuck in ``'dispatching'`` state.

P2-02 sub-task: When a worker crashes mid-dispatch, events can be left in
``publish_state = 'dispatching'`` indefinitely (the poller claimed them via
``FOR UPDATE SKIP LOCKED`` but the dispatcher never finalised them to
``'dispatched'``).

This sweeper detects those stuck events and resets them to ``'pending'`` so
the normal poll→dispatch loop can retry them safely.

Design
------
* Stuck threshold: events in ``'dispatching'`` for longer than
  ``worker_dispatching_timeout_seconds`` are considered orphaned.
* Recovery is a simple reset: ``publish_state = 'pending'``.
* Uses ``FOR UPDATE SKIP LOCKED`` so multiple workers can run the recovery
  sweep without conflict (though only the lease holder should invoke it).

Configuration
-------------
* ``worker_recovery_sweeper_interval_seconds`` (default 30)
* ``worker_dispatching_timeout_seconds`` (default 120)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from mneme.config import get_settings
from mneme.db.base import SessionLocal

logger = logging.getLogger(__name__)

# ── SQL Queries ──────────────────────────────────────────────────────────────

_FIND_STUCK_DISPATCHING = text("""
    SELECT event_id, event_type, committed_at
    FROM events
    WHERE publish_state = 'dispatching'
      AND committed_at < :threshold
    ORDER BY committed_at ASC
    LIMIT :limit
""")

_RESET_TO_PENDING = text("""
    UPDATE events
    SET publish_state = 'pending'
    WHERE event_id = :event_id
""")


class DispatchingRecoverySweeper:
    """Recovers events stuck in ``'dispatching'`` publish state.

    This is a companion to :class:`~mneme.worker.retry_sweeper.RetrySweeper`.
    Together they handle the two failure modes:

    * **Worker crash mid-poller** → event stuck in ``'dispatching'``
      → handled by this sweeper.
    * **Consumer returns failure** → ``delivery_state = 'failed'``
      → handled by the RetrySweeper.

    Usage::

        recovery = DispatchingRecoverySweeper(stuck_timeout_seconds=120)
        recovered = recovery.sweep()
        # recovered == number of events reset to 'pending'
    """

    def __init__(
        self,
        *,
        stuck_timeout_seconds: int = 120,
        batch_size: int = 50,
    ) -> None:
        self._timeout = stuck_timeout_seconds
        self._batch_size = batch_size

    def sweep(self) -> int:
        """Execute one recovery sweep cycle.

        Returns
        -------
        int
            Number of events recovered (reset to ``'pending'``).
        """
        threshold = datetime.now(timezone.utc) - timedelta(
            seconds=self._timeout
        )

        try:
            with SessionLocal() as db:
                rows = (
                    db.execute(
                        _FIND_STUCK_DISPATCHING,
                        {"threshold": threshold, "limit": self._batch_size},
                    )
                    .mappings()
                    .all()
                )

                if not rows:
                    return 0

                recovered = 0
                for row in rows:
                    event_id = row["event_id"]
                    event_type = row["event_type"]

                    db.execute(
                        _RESET_TO_PENDING,
                        {"event_id": event_id},
                    )

                    logger.warning(
                        "recovered stuck dispatching event – event_id=%s "
                        "event_type=%s timeout_s=%s",
                        event_id,
                        event_type,
                        self._timeout,
                    )
                    recovered += 1

                db.commit()
                return recovered

        except Exception as exc:
            logger.error("recovery sweeper cycle failed: %s", exc)
            return 0


def create_recovery_sweeper() -> DispatchingRecoverySweeper:
    """Create a :class:`DispatchingRecoverySweeper` from application settings."""
    settings = get_settings()
    return DispatchingRecoverySweeper(
        stuck_timeout_seconds=settings.worker_dispatching_timeout_seconds,
    )
