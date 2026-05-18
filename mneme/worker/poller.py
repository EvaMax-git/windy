"""Outbox poller – queries the ``events`` table for pending rows.

Phase 1 behaviour
-----------------

* Uses a CTE with ``SELECT … FOR UPDATE SKIP LOCKED`` that **atomically**
  transitions ``publish_state`` from ``'pending'`` to ``'dispatching'`` within
  the same transaction, then COMMITs.  This eliminates the double-dispatch
  race that existed in the prior implementation where ``rollback()`` released
  row locks before the dispatcher could claim the events.
* Returns lightweight data-classes so the dispatcher never touches raw
  SQLAlchemy rows directly.
* Once claimed, events are not visible to other worker pods (their state is
  already ``'dispatching'``).  The dispatcher is responsible for finalising
  the state to ``'dispatched'`` or recovering stuck ``'dispatching'`` events.

Phase 2+
--------

* Recovery sweeper for events stuck in ``'dispatching'`` state longer than a
  configurable timeout (e.g. worker crash mid-dispatch).
* When Redis is available the poller should acquire a distributed lease so
  that only one worker pod actively polls the outbox.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from mneme.db.base import SessionLocal

logger = logging.getLogger(__name__)

# ── Poll result data-class ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class PendingEvent:
    """A single event row that is ready for dispatch."""

    event_id: UUID
    event_type: str
    aggregate_type: str
    aggregate_id: UUID
    aggregate_version: int
    correlation_id: UUID
    causation_id: UUID | None
    idempotency_key: str
    producer: str
    payload_json: dict[str, Any] = field(default_factory=dict)
    visibility: str = "internal"
    occurred_at: datetime | None = None
    committed_at: datetime | None = None

    @property
    def event_id_str(self) -> str:
        return str(self.event_id)


# ── SQL ─────────────────────────────────────────────────────────────────────────

_FETCH_AND_CLAIM = text(
    """
    WITH locked AS (
        SELECT event_id
        FROM events
        WHERE publish_state = 'pending'
        ORDER BY committed_at
        LIMIT :limit
        FOR UPDATE SKIP LOCKED
    )
    UPDATE events
    SET publish_state = 'dispatching'
    FROM locked
    WHERE events.event_id = locked.event_id
    RETURNING
      events.event_id,
      events.event_type,
      events.aggregate_type,
      events.aggregate_id,
      events.aggregate_version,
      events.correlation_id,
      events.causation_id,
      events.idempotency_key,
      events.producer,
      events.payload_json,
      events.visibility,
      events.occurred_at,
      events.committed_at
    """
)


# ── Public API ──────────────────────────────────────────────────────────────────


def fetch_pending_events(
    *,
    limit: int = 20,
) -> list[PendingEvent]:
    """Return up to *limit* pending events from the outbox.

    Atomically transitions ``publish_state`` from ``'pending'`` to
    ``'dispatching'`` using ``FOR UPDATE SKIP LOCKED`` so that multiple
    worker pods can safely poll in parallel without double-dispatch risk.

    The state transition and the row data are committed in a single
    transaction.  Once claimed, the dispatcher is responsible for
    finalising ``publish_state`` to ``'dispatched'``.

    Parameters
    ----------
    limit : int
        Maximum number of events to fetch in one poll cycle.

    Returns
    -------
    list[PendingEvent]
        Ordered by ``committed_at`` ascending (oldest first).
    """
    db = SessionLocal()
    try:
        rows = db.execute(_FETCH_AND_CLAIM, {"limit": limit}).mappings().all()
        events: list[PendingEvent] = []
        for row in rows:
            payload = row["payload_json"] if isinstance(row["payload_json"], dict) else {}
            events.append(
                PendingEvent(
                    event_id=_coerce_uuid(row["event_id"]),
                    event_type=row["event_type"],
                    aggregate_type=row["aggregate_type"],
                    aggregate_id=_coerce_uuid(row["aggregate_id"]),
                    aggregate_version=int(row["aggregate_version"]),
                    correlation_id=_coerce_uuid(row["correlation_id"]),
                    causation_id=_coerce_uuid(row["causation_id"])
                    if row["causation_id"] is not None
                    else None,
                    idempotency_key=row["idempotency_key"],
                    producer=row["producer"],
                    payload_json=payload,
                    visibility=row["visibility"],
                    occurred_at=row["occurred_at"],
                    committed_at=row["committed_at"],
                )
            )
        db.commit()  # Persist publish_state='dispatching' and release locks
        return events
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _coerce_uuid(value: Any) -> UUID:
    if isinstance(value, UUID):
        return value
    return UUID(str(value))
