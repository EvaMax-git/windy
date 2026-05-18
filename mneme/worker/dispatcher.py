"""Event Dispatcher – routes outbox events to registered consumers.

Phase 1 behaviour
-----------------

* Maintains a registry of :class:`Consumer` implementations.
* For every pending event fetched from the outbox the dispatcher:
  1. Finds consumers whose ``can_handle(event_type)`` returns ``True``.
  2. Inserts or updates an ``event_deliveries`` row.
  3. Calls ``consumer.dispatch(event, delivery)``.
  4. On success updates ``event_deliveries.delivery_state`` to ``acknowledged``
     and the event ``publish_state`` to ``dispatched``.
  5. On failure records the error in ``event_deliveries.last_error`` and
     sets ``delivery_state`` to ``failed``.
* The default (and Phase 1 only) consumer is :class:`NoopConsumer`, which
  simply logs the event at INFO level.

Phase 2+
--------

* Real consumers (notification, webhook, pipeline, memory-index, …).
* Retry sweeper with exponential backoff and max-attempts.
* Dead-letter envelope for unrecoverable failures.
* Distributed lease / leader election (Redis-based).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session

from mneme.db.base import SessionLocal

logger = logging.getLogger(__name__)


# ── Dispatch Result ─────────────────────────────────────────────────────────────


class DispatchOutcome(str, Enum):
    acknowledged = "acknowledged"
    failed = "failed"


@dataclass
class DispatchResult:
    """Returned by :meth:`Consumer.dispatch` after processing an event."""

    outcome: DispatchOutcome
    error: str | None = None

    @classmethod
    def ack(cls) -> DispatchResult:
        return cls(outcome=DispatchOutcome.acknowledged)

    @classmethod
    def fail(cls, error: str) -> DispatchResult:
        return cls(outcome=DispatchOutcome.failed, error=error)


# ── Consumer Interface ──────────────────────────────────────────────────────────


class Consumer(ABC):
    """Abstract consumer that receives dispatched events.

    Implementations are stateless: one instance is registered at startup and
    the dispatcher calls :meth:`dispatch` for every matching event.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique consumer name, used as ``event_deliveries.consumer_name``."""
        ...

    @abstractmethod
    def can_handle(self, event_type: str) -> bool:
        """Return ``True`` if this consumer should receive *event_type*."""
        ...

    @abstractmethod
    def dispatch(
        self,
        *,
        event_id: UUID,
        event_type: str,
        aggregate_type: str,
        aggregate_id: UUID,
        payload: dict[str, Any],
        delivery_id: UUID,
    ) -> DispatchResult:
        """Process a single event.

        The implementation MUST NOT raise exceptions.  Any failure should be
        returned as :class:`DispatchResult` with ``outcome=failed`` and an
        ``error`` message.
        """
        ...


# ── No-op Consumer ──────────────────────────────────────────────────────────────


class NoopConsumer(Consumer):
    """Phase 1 consumer that logs every event and always acknowledges.

    This serves as the sole consumer in Phase 1, proving that the
    dispatcher loop, outbox polling, and event_deliveries tracking work
    end-to-end.
    """

    @property
    def name(self) -> str:
        return "noop"

    def can_handle(self, event_type: str) -> bool:
        return True

    def dispatch(
        self,
        *,
        event_id: UUID,
        event_type: str,
        aggregate_type: str,
        aggregate_id: UUID,
        payload: dict[str, Any],
        delivery_id: UUID,
    ) -> DispatchResult:
        logger.info(
            "noop dispatch – event_id=%s event_type=%s aggregate_type=%s "
            "aggregate_id=%s delivery_id=%s",
            event_id,
            event_type,
            aggregate_type,
            aggregate_id,
            delivery_id,
        )
        return DispatchResult.ack()


# ── SQL Helpers ─────────────────────────────────────────────────────────────────

_INSERT_OR_GET_DELIVERY = text(
    """
    INSERT INTO event_deliveries (
      event_id,
      consumer_name,
      delivery_state,
      dispatch_attempts,
      last_dispatched_at
    )
    VALUES (
      :event_id,
      :consumer_name,
      'dispatched',
      1,
      now()
    )
    ON CONFLICT (event_id, consumer_name) DO UPDATE
    SET delivery_state   = CASE
          WHEN event_deliveries.delivery_state = 'dead_letter' THEN 'dead_letter'
          ELSE 'dispatched'
        END,
        dispatch_attempts = event_deliveries.dispatch_attempts + 1,
        last_dispatched_at = now(),
        updated_at = now()
    RETURNING delivery_id, delivery_state, dispatch_attempts
    """
)


_ACK_DELIVERY = text(
    """
    UPDATE event_deliveries
    SET delivery_state   = 'acknowledged',
        acknowledged_at  = now(),
        updated_at       = now()
    WHERE delivery_id = :delivery_id
    """
)


_FAIL_DELIVERY = text(
    """
    UPDATE event_deliveries
    SET delivery_state   = 'failed',
        last_error       = :last_error,
        failed_at        = now(),
        updated_at       = now()
    WHERE delivery_id = :delivery_id
    """
)


_MARK_EVENT_DISPATCHED = text(
    """
    UPDATE events
    SET publish_state  = 'dispatched',
        published_at   = now()
    WHERE event_id     = :event_id
    """
)


# ── Dispatcher ──────────────────────────────────────────────────────────────────


class Dispatcher:
    """Registry-driven event dispatcher.

    Usage::

        dispatcher = Dispatcher()
        dispatcher.register(NoopConsumer())
        dispatcher.dispatch_pending(pending_events)

    """

    def __init__(self) -> None:
        self._consumers: list[Consumer] = []

    def register(self, consumer: Consumer) -> None:
        """Register a consumer that will receive matching events."""
        if any(c.name == consumer.name for c in self._consumers):
            raise ValueError(f"consumer '{consumer.name}' already registered")
        self._consumers.append(consumer)

    @property
    def consumers(self) -> list[Consumer]:
        return list(self._consumers)

    def dispatch_pending(
        self,
        pending_events: list[Any],
    ) -> int:
        """Process a batch of pending events.

        Parameters
        ----------
        pending_events : list[PendingEvent]
            Events fetched by the outbox poller.

        Returns
        -------
        int
            Total number of consumer invocations performed.
        """
        if not pending_events:
            return 0

        total_dispatched = 0

        with SessionLocal() as db:
            try:
                for event in pending_events:
                    dispatched = self._dispatch_one(db, event)
                    total_dispatched += dispatched
                db.commit()
            except Exception:
                db.rollback()
                raise

        return total_dispatched

    # ── Internals ───────────────────────────────────────────────────────────

    def _matching_consumers(self, event_type: str) -> list[Consumer]:
        return [c for c in self._consumers if c.can_handle(event_type)]

    def _dispatch_one(self, db: Session, event: Any) -> int:
        """Dispatch a single event to all matching consumers."""
        event_type = event.event_type
        event_id = event.event_id
        consumers = self._matching_consumers(event_type)

        if not consumers:
            logger.debug(
                "no consumers for event_id=%s event_type=%s",
                event_id,
                event_type,
            )
            return 0

        count = 0
        for consumer in consumers:
            delivery = _ensure_delivery(
                db, event_id=event_id, consumer_name=consumer.name
            )

            try:
                result = consumer.dispatch(
                    event_id=event_id,
                    event_type=event_type,
                    aggregate_type=event.aggregate_type,
                    aggregate_id=event.aggregate_id,
                    payload=event.payload_json,
                    delivery_id=_coerce_uuid(delivery["delivery_id"]),
                )
            except Exception as exc:
                result = DispatchResult.fail(str(exc))

            if result.outcome == DispatchOutcome.acknowledged:
                _update_delivery_ack(db, delivery_id=_coerce_uuid(delivery["delivery_id"]))
            else:
                error_msg = result.error or "unknown error"
                logger.warning(
                    "dispatch failed – event_id=%s consumer=%s error=%s",
                    event_id,
                    consumer.name,
                    error_msg,
                )
                _update_delivery_fail(
                    db,
                    delivery_id=_coerce_uuid(delivery["delivery_id"]),
                    error=error_msg[:2000],
                )

            count += 1

        _update_event_dispatched(db, event_id=event_id)

        return count


# ── Internal helpers ────────────────────────────────────────────────────────────


def _ensure_delivery(
    db: Session, *, event_id: UUID, consumer_name: str
) -> dict[str, Any]:
    """Insert or update an ``event_deliveries`` row, returning key columns."""
    row = (
        db.execute(
            _INSERT_OR_GET_DELIVERY,
            {"event_id": event_id, "consumer_name": consumer_name},
        )
        .mappings()
        .one()
    )
    return {
        "delivery_id": row["delivery_id"],
        "delivery_state": row["delivery_state"],
        "dispatch_attempts": row["dispatch_attempts"],
    }


def _update_delivery_ack(db: Session, *, delivery_id: UUID) -> None:
    db.execute(_ACK_DELIVERY, {"delivery_id": delivery_id})


def _update_delivery_fail(db: Session, *, delivery_id: UUID, error: str) -> None:
    db.execute(
        _FAIL_DELIVERY,
        {"delivery_id": delivery_id, "last_error": error},
    )


def _update_event_dispatched(db: Session, *, event_id: UUID) -> None:
    db.execute(_MARK_EVENT_DISPATCHED, {"event_id": event_id})


def _coerce_uuid(value: Any) -> UUID:
    if isinstance(value, UUID):
        return value
    return UUID(str(value))
