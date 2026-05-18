"""P2-07 Review Event Consumer — outbox-driven review workflow follow-up actions.

This consumer is registered with the :class:`~mneme.worker.dispatcher.Dispatcher`
and handles review lifecycle events published to the outbox:

* ``review.created``   — log, no-op for now (metrics placeholder)
* ``review.claimed``   — log, no-op for now
* ``review.approved``  — trigger follow-up action based on ``review_type``
* ``review.rejected``  — cleanup / cancel associated resources
* ``review.cancelled`` — cleanup / cancel associated resources
* ``review.expired``   — log, no-op for now (escalation placeholder)

The consumer is **idempotent**: re-delivery of the same outbox event is safe
because follow-up actions check the current state before acting (e.g. DLQ
replay verifies ``replay_state = 'under_review'`` before updating).

Architecture
------------
The API route (``review_items.py``) writes the outbox event and may also
execute the follow-up inline.  The consumer provides the **outbox-driven**
path that guarantees eventual execution even if the API process crashes
after writing the event but before completing the inline action.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import text

from mneme.db.base import SessionLocal
from mneme.worker.dispatcher import Consumer, DispatchResult, DispatchOutcome

logger = logging.getLogger(__name__)

# ── Event types this consumer handles ─────────────────────────────────────────

_HANDLED_EVENT_TYPES = frozenset(
    {
        "review.created",
        "review.claimed",
        "review.approved",
        "review.rejected",
        "review.cancelled",
        "review.expired",
    }
)


class ReviewEventConsumer(Consumer):
    """Outbox consumer for review lifecycle events.

    Registered in the worker's Dispatcher, this consumer receives
    review events and triggers the appropriate follow-up actions.

    For ``review.approved`` events, the consumer checks ``review_type``
    in the payload and dispatches to the correct handler:

    * ``dlq_replay`` → :meth:`_handle_dlq_replay_approved`
    * ``restore_confirm`` → :meth:`_handle_restore_approved` (stub)
    * ``sensitive_access`` → :meth:`_handle_sensitive_access_approved` (stub)
    * ``high_cost_call`` → :meth:`_handle_high_cost_call_approved` (stub)

    All handler methods are idempotent and safe for re-delivery.
    """

    @property
    def name(self) -> str:
        return "review-consumer"

    def can_handle(self, event_type: str) -> bool:
        """Return ``True`` for all review.* event types."""
        return event_type in _HANDLED_EVENT_TYPES

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
        """Route a review outbox event to the appropriate handler.

        Parameters
        ----------
        event_id : UUID
            The ``events.event_id`` of the outbox row.
        event_type : str
            One of ``review.created``, ``review.approved``, etc.
        aggregate_type : str
            Always ``"review_item"`` for review events.
        aggregate_id : UUID
            The ``review_items.review_item_id``.
        payload : dict
            Contains ``review_type``, ``target_type``, ``target_id``,
            ``request_id``, ``correlation_id``.
        delivery_id : UUID
            The ``event_deliveries.delivery_id`` for this delivery attempt.

        Returns
        -------
        DispatchResult
            ``ack`` on success, ``fail`` with an error message on failure.
        """
        review_item_id = aggregate_id
        review_type = payload.get("review_type", "unknown")
        target_type = payload.get("target_type", "unknown")
        target_id_raw = payload.get("target_id")
        target_id = UUID(target_id_raw) if target_id_raw else None

        log_ctx = dict(
            event_id=str(event_id),
            event_type=event_type,
            review_item_id=str(review_item_id),
            review_type=review_type,
            target_type=target_type,
            target_id=str(target_id) if target_id else None,
            delivery_id=str(delivery_id),
        )

        try:
            if event_type == "review.created":
                return self._handle_created(review_item_id, payload, log_ctx)
            elif event_type == "review.claimed":
                return self._handle_claimed(review_item_id, payload, log_ctx)
            elif event_type == "review.approved":
                return self._handle_approved(
                    review_item_id, review_type, target_type, target_id, payload, log_ctx
                )
            elif event_type == "review.rejected":
                return self._handle_rejected(
                    review_item_id, review_type, target_type, target_id, payload, log_ctx
                )
            elif event_type == "review.cancelled":
                return self._handle_cancelled(
                    review_item_id, review_type, target_type, target_id, payload, log_ctx
                )
            elif event_type == "review.expired":
                return self._handle_expired(review_item_id, payload, log_ctx)
            else:
                logger.warning("review-consumer: unhandled event_type=%s", event_type)
                return DispatchResult.ack()
        except Exception as exc:
            logger.error(
                "review-consumer: dispatch failed – %s error=%s",
                log_ctx,
                exc,
                exc_info=True,
            )
            return DispatchResult.fail(str(exc)[:2000])

    # ── Event handlers ──────────────────────────────────────────────────────

    def _handle_created(
        self,
        review_item_id: UUID,
        payload: dict[str, Any],
        log_ctx: dict,
    ) -> DispatchResult:
        """Handle ``review.created`` — log and ack (metrics placeholder)."""
        logger.info(
            "review.created – review_item=%s type=%s target=%s",
            review_item_id,
            payload.get("review_type"),
            payload.get("target_type"),
        )
        return DispatchResult.ack()

    def _handle_claimed(
        self,
        review_item_id: UUID,
        payload: dict[str, Any],
        log_ctx: dict,
    ) -> DispatchResult:
        """Handle ``review.claimed`` — log and ack."""
        logger.info(
            "review.claimed – review_item=%s type=%s",
            review_item_id,
            payload.get("review_type"),
        )
        return DispatchResult.ack()

    def _handle_approved(
        self,
        review_item_id: UUID,
        review_type: str,
        target_type: str,
        target_id: UUID | None,
        payload: dict[str, Any],
        log_ctx: dict,
    ) -> DispatchResult:
        """Handle ``review.approved`` — route to type-specific handler."""
        logger.info(
            "review.approved – review_item=%s review_type=%s target_type=%s",
            review_item_id,
            review_type,
            target_type,
        )

        if review_type == "dlq_replay":
            return self._handle_dlq_replay_approved(
                review_item_id, target_id, log_ctx
            )
        elif review_type == "restore_confirm":
            return self._handle_restore_approved(
                review_item_id, target_id, log_ctx
            )
        elif review_type == "sensitive_access":
            return self._handle_sensitive_access_approved(
                review_item_id, target_id, log_ctx
            )
        elif review_type == "high_cost_call":
            return self._handle_high_cost_call_approved(
                review_item_id, target_id, log_ctx
            )
        else:
            logger.info(
                "review.approved – no follow-up for review_type=%s, ack",
                review_type,
            )
            return DispatchResult.ack()

    def _handle_rejected(
        self,
        review_item_id: UUID,
        review_type: str,
        target_type: str,
        target_id: UUID | None,
        payload: dict[str, Any],
        log_ctx: dict,
    ) -> DispatchResult:
        """Handle ``review.rejected`` — cleanup/cancel associated resources."""
        logger.info(
            "review.rejected – review_item=%s review_type=%s target_type=%s",
            review_item_id,
            review_type,
            target_type,
        )

        if review_type == "dlq_replay" and target_id:
            return self._cancel_dlq_replay(review_item_id, target_id, log_ctx)
        elif review_type == "restore_confirm":
            logger.info(
                "review.rejected – restore cancelled review_item=%s",
                review_item_id,
            )
        elif review_type == "sensitive_access":
            logger.info(
                "review.rejected – sensitive_access denied review_item=%s",
                review_item_id,
            )
        elif review_type == "high_cost_call":
            logger.info(
                "review.rejected – high_cost_call denied review_item=%s",
                review_item_id,
            )

        return DispatchResult.ack()

    def _handle_cancelled(
        self,
        review_item_id: UUID,
        review_type: str,
        target_type: str,
        target_id: UUID | None,
        payload: dict[str, Any],
        log_ctx: dict,
    ) -> DispatchResult:
        """Handle ``review.cancelled`` — cleanup/cancel associated resources."""
        logger.info(
            "review.cancelled – review_item=%s review_type=%s target_type=%s",
            review_item_id,
            review_type,
            target_type,
        )

        if review_type == "dlq_replay" and target_id:
            return self._cancel_dlq_replay(review_item_id, target_id, log_ctx)

        return DispatchResult.ack()

    def _handle_expired(
        self,
        review_item_id: UUID,
        payload: dict[str, Any],
        log_ctx: dict,
    ) -> DispatchResult:
        """Handle ``review.expired`` — log and ack (escalation placeholder)."""
        logger.warning(
            "review.expired – review_item=%s type=%s (escalation not implemented)",
            review_item_id,
            payload.get("review_type"),
        )
        return DispatchResult.ack()

    # ── Type-specific follow-up handlers ────────────────────────────────────

    def _handle_dlq_replay_approved(
        self,
        review_item_id: UUID,
        dead_letter_id: UUID | None,
        log_ctx: dict,
    ) -> DispatchResult:
        """Execute DLQ replay after review approval.

        Idempotent: checks ``replay_state = 'under_review'`` before updating.
        If the DLQ replay was already executed inline by the API route, this
        is a safe no-op.
        """
        if dead_letter_id is None:
            logger.error(
                "review.approved dlq_replay – missing target_id (dead_letter_id) "
                "review_item=%s",
                review_item_id,
            )
            return DispatchResult.fail("missing target_id for dlq_replay")

        _UPDATE_DL_REPLAYED = text("""
            UPDATE dead_letters
            SET replay_state = 'replayed',
                replayed_at = now(),
                updated_at = now()
            WHERE dead_letter_id = :dead_letter_id
              AND replay_state = 'under_review'
        """)

        _GET_SOURCE_DELIVERY = text("""
            SELECT source_id
            FROM dead_letters
            WHERE dead_letter_id = :dead_letter_id
              AND source_type = 'event_delivery'
        """)

        _RESET_DELIVERY_FOR_REPLAY = text("""
            UPDATE event_deliveries
            SET dispatch_attempts = 0,
                delivery_state = 'pending',
                last_error = NULL,
                failed_at = NULL,
                lease_expires_at = NULL,
                updated_at = now()
            WHERE delivery_id = :delivery_id
        """)

        _GET_RELATED_EVENT = text("""
            SELECT related_event_id
            FROM dead_letters
            WHERE dead_letter_id = :dead_letter_id
        """)

        _RESET_EVENT_FOR_REPLAY = text("""
            UPDATE events
            SET publish_state = 'pending',
                last_error = NULL,
                updated_at = now()
            WHERE event_id = :event_id
        """)

        with SessionLocal() as db:
            try:
                # 1. Update dead_letter replay_state (idempotent guard)
                result = db.execute(
                    _UPDATE_DL_REPLAYED,
                    {"dead_letter_id": dead_letter_id},
                )
                if result.rowcount == 0:
                    logger.info(
                        "DLQ replay (consumer): dead_letter %s not in "
                        "'under_review' state (already replayed?), skip",
                        dead_letter_id,
                    )
                    db.commit()
                    return DispatchResult.ack()

                # 2. Reset the event_delivery for re-dispatch
                source_delivery = (
                    db.execute(
                        _GET_SOURCE_DELIVERY,
                        {"dead_letter_id": dead_letter_id},
                    )
                    .mappings()
                    .first()
                )

                if source_delivery:
                    delivery_id = source_delivery["source_id"]
                    db.execute(
                        _RESET_DELIVERY_FOR_REPLAY,
                        {"delivery_id": delivery_id},
                    )
                    logger.info(
                        "DLQ replay (consumer): reset delivery %s for "
                        "dead_letter %s",
                        delivery_id,
                        dead_letter_id,
                    )

                # 3. Reset the related event
                related = (
                    db.execute(
                        _GET_RELATED_EVENT,
                        {"dead_letter_id": dead_letter_id},
                    )
                    .mappings()
                    .first()
                )

                if related and related["related_event_id"]:
                    event_id = related["related_event_id"]
                    db.execute(
                        _RESET_EVENT_FOR_REPLAY,
                        {"event_id": event_id},
                    )
                    logger.info(
                        "DLQ replay (consumer): reset event %s for "
                        "dead_letter %s",
                        event_id,
                        dead_letter_id,
                    )

                db.commit()
                logger.info(
                    "DLQ replay (consumer): completed dead_letter=%s "
                    "review_item=%s",
                    dead_letter_id,
                    review_item_id,
                )
                return DispatchResult.ack()

            except Exception:
                db.rollback()
                raise

    def _handle_restore_approved(
        self,
        review_item_id: UUID,
        restore_target_id: UUID | None,
        log_ctx: dict,
    ) -> DispatchResult:
        """Handle restore confirmation approval (stub for P2-16 integration).

        In Phase 2, this is a placeholder.  The actual restore execution will
        be triggered by the Backup/Restore management API (P2-16).
        """
        logger.info(
            "review.approved restore_confirm – restore execution stub "
            "review_item=%s",
            review_item_id,
        )
        return DispatchResult.ack()

    def _handle_sensitive_access_approved(
        self,
        review_item_id: UUID,
        target_id: UUID | None,
        log_ctx: dict,
    ) -> DispatchResult:
        """Handle sensitive access approval (placeholder).

        In future phases this may grant temporary access or log a
        time-limited permission.  For Phase 2 it is a no-op.
        """
        logger.info(
            "review.approved sensitive_access – no-op stub review_item=%s",
            review_item_id,
        )
        return DispatchResult.ack()

    def _handle_high_cost_call_approved(
        self,
        review_item_id: UUID,
        target_id: UUID | None,
        log_ctx: dict,
    ) -> DispatchResult:
        """Handle high-cost call approval (placeholder).

        In Phase 2 this may allow a blocked Gateway call to proceed.
        For now it is a no-op.
        """
        logger.info(
            "review.approved high_cost_call – no-op stub review_item=%s",
            review_item_id,
        )
        return DispatchResult.ack()

    # ── Cancellation helpers ────────────────────────────────────────────────

    def _cancel_dlq_replay(
        self,
        review_item_id: UUID,
        dead_letter_id: UUID,
        log_ctx: dict,
    ) -> DispatchResult:
        """Cancel a DLQ replay — reset ``dead_letters.replay_state`` to
        ``'cancelled'``.

        Idempotent: only acts when ``replay_state = 'under_review'``.
        """
        _CANCEL_DL_REPLAY = text("""
            UPDATE dead_letters
            SET replay_state = 'cancelled',
                updated_at = now()
            WHERE dead_letter_id = :dead_letter_id
              AND replay_state = 'under_review'
        """)

        with SessionLocal() as db:
            try:
                result = db.execute(
                    _CANCEL_DL_REPLAY,
                    {"dead_letter_id": dead_letter_id},
                )
                if result.rowcount == 0:
                    logger.info(
                        "DLQ cancel (consumer): dead_letter %s not in "
                        "'under_review' state, skip",
                        dead_letter_id,
                    )
                else:
                    logger.info(
                        "DLQ replay cancelled (consumer): dead_letter=%s "
                        "review_item=%s",
                        dead_letter_id,
                        review_item_id,
                    )
                db.commit()
                return DispatchResult.ack()
            except Exception:
                db.rollback()
                raise
