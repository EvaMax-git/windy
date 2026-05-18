from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, TypeVar
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mneme.api.context import RequestContext
from mneme.db.idempotency import IdempotencyConflict, check_idempotency_key
from mneme.db.transactions import transaction


T = TypeVar("T")


@dataclass(frozen=True)
class AuditEvent:
    action: str
    result: str = "success"
    object_type: str | None = None
    object_id: UUID | None = None
    project_id: UUID | None = None
    reason_code: str | None = None
    sensitivity_level: str = "normal"
    review_item_id: UUID | None = None
    diff_summary: dict[str, Any] = field(default_factory=dict)
    metadata_json: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OutboxEvent:
    event_type: str
    aggregate_type: str
    aggregate_id: UUID
    aggregate_version: int
    idempotency_key: str
    producer: str = "mneme-api"
    payload_json: dict[str, Any] = field(default_factory=dict)
    visibility: str = "internal"
    publish_state: str = "pending"
    causation_id: UUID | None = None
    occurred_at: datetime | None = None


_INSERT_AUDIT_EVENT = text(
    """
    INSERT INTO audit_events (
      audit_id,
      actor_type,
      actor_id,
      auth_context_type,
      auth_context_id,
      action,
      object_type,
      object_id,
      project_id,
      result,
      reason_code,
      sensitivity_level,
      correlation_id,
      request_id,
      review_item_id,
      diff_summary,
      metadata_json
    )
    VALUES (
      :audit_id,
      :actor_type,
      :actor_id,
      :auth_context_type,
      :auth_context_id,
      :action,
      :object_type,
      :object_id,
      :project_id,
      :result,
      :reason_code,
      :sensitivity_level,
      :correlation_id,
      :request_id,
      :review_item_id,
      :diff_summary,
      :metadata_json
    )
    RETURNING audit_id
    """
)

_INSERT_OUTBOX_EVENT = text(
    """
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
    RETURNING event_id
    """
)


def _as_uuid(value: Any) -> UUID:
    """Coerce a value to :class:`uuid.UUID`.

    PostgreSQL's psycopg2 driver returns Python ``UUID`` objects natively.
    SQLite returns hex strings (without dashes) when the column type is
    declared as ``UUID``.  This helper normalises both paths.
    """
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def add_audit_event(db: Session, context: RequestContext, event: AuditEvent) -> UUID:
    audit_id = uuid4()
    db.execute(
        _INSERT_AUDIT_EVENT,
        {
            "audit_id": audit_id,
            "actor_type": context.actor.actor_type,
            "actor_id": context.actor.actor_id,
            "auth_context_type": context.actor.auth_context_type,
            "auth_context_id": context.actor.auth_context_id,
            "action": event.action,
            "object_type": event.object_type,
            "object_id": event.object_id,
            "project_id": event.project_id,
            "result": event.result,
            "reason_code": event.reason_code,
            "sensitivity_level": event.sensitivity_level,
            "correlation_id": context.correlation_id,
            "request_id": context.request_id,
            "review_item_id": event.review_item_id,
            "diff_summary": json.dumps(event.diff_summary) if isinstance(event.diff_summary, dict) else event.diff_summary,
            "metadata_json": json.dumps(event.metadata_json) if isinstance(event.metadata_json, dict) else event.metadata_json,
        },
    )
    return audit_id


def add_outbox_event(db: Session, context: RequestContext, event: OutboxEvent) -> UUID:
    payload = {
        "request_id": str(context.request_id),
        "correlation_id": str(context.correlation_id),
        **event.payload_json,
    }
    event_id = uuid4()
    db.execute(
        _INSERT_OUTBOX_EVENT,
        {
            "event_id": event_id,
            "event_type": event.event_type,
            "aggregate_type": event.aggregate_type,
            "aggregate_id": event.aggregate_id,
            "aggregate_version": event.aggregate_version,
            "correlation_id": context.correlation_id,
            "causation_id": event.causation_id,
            "idempotency_key": event.idempotency_key,
            "producer": event.producer,
            "payload_json": json.dumps(payload) if isinstance(payload, dict) else payload,
            "visibility": event.visibility,
            "publish_state": event.publish_state,
            "occurred_at": event.occurred_at or datetime.now(timezone.utc),
        },
    )
    return event_id


def add_audit_and_outbox(
    db: Session,
    context: RequestContext,
    *,
    audit_event: AuditEvent,
    outbox_event: OutboxEvent,
) -> tuple[UUID, UUID]:
    audit_id = add_audit_event(db, context, audit_event)
    event_id = add_outbox_event(db, context, outbox_event)
    return audit_id, event_id


def write_with_audit_and_outbox(
    db: Session,
    context: RequestContext,
    *,
    work: Callable[[Session], T],
    audit_event: AuditEvent,
    outbox_event: OutboxEvent,
    on_success: Callable[[Session, UUID, UUID], None] | None = None,
) -> T:
    """Execute *work*, then write audit + outbox rows, all in one transaction.

    Parameters
    ----------
    on_success : Callable[[Session, UUID, UUID], None] | None
        Optional callback invoked *after* audit and outbox rows are inserted
        but *before* the transaction commits.  Receives the active session,
        ``audit_id``, and ``event_id``.  Use this to write cross-referencing
        rows (e.g. ``object_versions`` with ``audit_id`` and ``event_id``
        foreign keys).
    """
    with transaction(db):
        result = work(db)
        audit_id, event_id = add_audit_and_outbox(
            db,
            context,
            audit_event=audit_event,
            outbox_event=outbox_event,
        )
        if on_success is not None:
            on_success(db, audit_id, event_id)
        return result


def write_with_audit_outbox_idempotency(
    db: Session,
    context: RequestContext,
    *,
    work: Callable[[Session], T],
    audit_event: AuditEvent,
    outbox_event: OutboxEvent,
    resolve_existing: Callable[[Session, UUID], T] | None = None,
    on_success: Callable[[Session, UUID, UUID], None] | None = None,
) -> T:
    """Execute *work* inside a transaction that also writes audit + outbox,
    guarded by an idempotency check.

    Flow
    ----

    1. **Pre-check** – If ``outbox_event.idempotency_key`` is set, query ``events``
       for an existing record with the same key and aggregate type.  If found,
       call *resolve_existing* to return the previously created object.

    2. **Write** – Run *work* and then insert ``audit_events`` and ``events``
       in the same transaction via :func:`write_with_audit_and_outbox`.

    3. **Race handling** – If a concurrent request sneaks in between the
       pre-check and the INSERT, the ``UNIQUE(idempotency_key)`` constraint
       raises ``IntegrityError``.  The helper catches it, rolls back, and
       re-resolves via *resolve_existing*.

    Parameters
    ----------
    db : Session
        Active SQLAlchemy session.
    context : RequestContext
        Must carry ``idempotency_key`` for idempotent writes.
    work : Callable[[Session], T]
        Callback that performs the business-table writes and returns the
        result object.  Receives the same ``db`` session.
    audit_event : AuditEvent
        Audit record to write on success.
    outbox_event : OutboxEvent
        Outbox event to write on success.  Its ``idempotency_key`` is used
        for the pre-check and stored in the ``events`` table.
    resolve_existing : Callable[[Session, UUID], T] | None
        Called when the idempotency key already exists.  Receives the
        ``aggregate_id`` from the existing event and must return the
        previously created business object.
    on_success : Callable[[Session, UUID, UUID], None] | None
        Optional callback invoked *after* audit + outbox rows are inserted
        but *before* the transaction commits.  Receives the active session,
        ``audit_id``, and ``event_id``.

    Returns
    -------
    T
        The result of *work* (new write) or *resolve_existing* (idempotent replay).

    Raises
    ------
    IdempotencyConflict
        If the idempotency key exists but no *resolve_existing* callback was
        supplied, or if a concurrent-duplicate race is lost and re-resolution
        also fails.
    """
    idempotency_key = outbox_event.idempotency_key

    # ── 1. Pre-check ──────────────────────────────────────────────────────
    if idempotency_key:
        existing_aggregate_id = check_idempotency_key(
            db,
            idempotency_key=idempotency_key,
            aggregate_type=outbox_event.aggregate_type,
        )
        if existing_aggregate_id is not None:
            if resolve_existing is None:
                raise IdempotencyConflict(
                    idempotency_key,
                    outbox_event.aggregate_type,
                )
            return resolve_existing(db, existing_aggregate_id)

    # ── 2. Write ──────────────────────────────────────────────────────────
    try:
        return write_with_audit_and_outbox(
            db,
            context,
            work=work,
            audit_event=audit_event,
            outbox_event=outbox_event,
            on_success=on_success,
        )
    except IntegrityError:
        # ── 3. Race: someone else wrote the same key concurrently ─────────
        if idempotency_key and resolve_existing is not None:
            # The failed transaction is already rolled back by the
            # transaction() context manager inside write_with_audit_and_outbox.
            # Re-check in the current (now clean) session.
            existing_aggregate_id = check_idempotency_key(
                db,
                idempotency_key=idempotency_key,
                aggregate_type=outbox_event.aggregate_type,
            )
            if existing_aggregate_id is not None:
                return resolve_existing(db, existing_aggregate_id)
        raise
