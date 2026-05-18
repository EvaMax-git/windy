"""Security-level audit event and outbox event helpers.

This module translates domain-level events (policy decisions, auth results,
action outcomes) into :class:`mneme.db.audit.AuditEvent` and
:class:`mneme.db.audit.OutboxEvent` dataclass instances, and provides
high-level write helpers that construct **and** write these records.

The separation of concerns is:

* ``mneme/security/audit.py``   — *what* to audit / publish (classification,
                                  reason mapping, event construction) **and**
                                  high-level write helpers that combine
                                  construction + persistence.
* ``mneme/db/audit.py``         — *how* to write records (raw SQL,
                                  transactions, idempotency guards).

Route handlers should prefer the ``emit_*`` helpers in this module over
calling ``mneme.db.audit`` functions directly.  The ``emit_*`` helpers
ensure consistent field population (actor, request_id, correlation_id) and
proper audit classification.

Factories
---------

.. autosummary::

    audit_event_for_action
    audit_event_for_auth
    audit_event_for_policy_denied
    audit_event_for_policy_review_required
    audit_event_for_policy_step_up_required
    outbox_event_for_action

Write helpers (construct + write in one call)
----------------------------------------------

.. autosummary::

    emit_audit_event
    emit_outbox_event
    emit_audit_and_outbox

Transactional write helpers (re-exported from :mod:`mneme.db.audit`)
----------------------------------------------------------------------

.. autosummary::

    write_with_audit_and_outbox
    write_with_audit_outbox_idempotency
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, TypeVar
from uuid import UUID

from sqlalchemy.orm import Session

from mneme.api.context import RequestContext
from mneme.security.policy import PolicyDecision
from mneme.db.audit import (
    AuditEvent,
    OutboxEvent,
    add_audit_event,
    add_outbox_event,
    add_audit_and_outbox as _db_add_audit_and_outbox,
    write_with_audit_and_outbox,
    write_with_audit_outbox_idempotency,
)

T = TypeVar("T")


# ═══════════════════════════════════════════════════════════════════════════════
# Factories: AuditEvent
# ═══════════════════════════════════════════════════════════════════════════════


def audit_event_for_policy_denied(
    *,
    action: str,
    decision: PolicyDecision,
    object_type: str | None = None,
    object_id: UUID | None = None,
    project_id: UUID | None = None,
) -> AuditEvent:
    """Build an :class:`AuditEvent` for a policy-deny decision.

    The ``reason_code`` is derived from ``decision.deny_reason`` and the
    ``metadata_json`` includes the human-readable message.
    """
    return AuditEvent(
        action=action,
        result="denied",
        object_type=object_type,
        object_id=object_id,
        project_id=project_id,
        reason_code=decision.deny_reason.value if decision.deny_reason else None,
        metadata_json=_policy_metadata(decision),
    )


def audit_event_for_policy_review_required(
    *,
    action: str,
    decision: PolicyDecision,
    object_type: str | None = None,
    object_id: UUID | None = None,
    project_id: UUID | None = None,
    review_item_id: UUID | None = None,
) -> AuditEvent:
    """Build an :class:`AuditEvent` for a review_required decision."""
    return AuditEvent(
        action=action,
        result="denied",
        object_type=object_type,
        object_id=object_id,
        project_id=project_id,
        reason_code=decision.deny_reason.value if decision.deny_reason else None,
        review_item_id=review_item_id,
        metadata_json=_policy_metadata(decision),
    )


def audit_event_for_policy_step_up_required(
    *,
    action: str,
    decision: PolicyDecision,
    object_type: str | None = None,
    object_id: UUID | None = None,
    project_id: UUID | None = None,
) -> AuditEvent:
    """Build an :class:`AuditEvent` for a step_up_required decision."""
    return AuditEvent(
        action=action,
        result="denied",
        object_type=object_type,
        object_id=object_id,
        project_id=project_id,
        reason_code=decision.deny_reason.value if decision.deny_reason else None,
        metadata_json=_policy_metadata(decision),
    )


def audit_event_for_action(
    *,
    action: str,
    result: str = "success",
    object_type: str | None = None,
    object_id: UUID | None = None,
    project_id: UUID | None = None,
    reason_code: str | None = None,
    sensitivity_level: str = "normal",
    diff_summary: dict[str, Any] | None = None,
    metadata_json: dict[str, Any] | None = None,
) -> AuditEvent:
    """Build a generic :class:`AuditEvent` for a successful or failed action.

    This is the most common factory used by CRUD helpers: it takes the raw
    field values and constructs a properly typed ``AuditEvent``.
    """
    return AuditEvent(
        action=action,
        result=result,
        object_type=object_type,
        object_id=object_id,
        project_id=project_id,
        reason_code=reason_code,
        sensitivity_level=sensitivity_level,
        diff_summary=diff_summary or {},
        metadata_json=metadata_json or {},
    )


def audit_event_for_auth(
    *,
    action: str,
    result: str,
    object_type: str | None = None,
    object_id: UUID | None = None,
    reason_code: str | None = None,
    metadata_json: dict[str, Any] | None = None,
) -> AuditEvent:
    """Build an :class:`AuditEvent` for an authentication-related action.

    Convenience wrapper used by ``login`` / ``logout`` / token exchange flows.
    """
    return AuditEvent(
        action=action,
        result=result,
        object_type=object_type,
        object_id=object_id,
        reason_code=reason_code,
        metadata_json=metadata_json or {},
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Factories: OutboxEvent
# ═══════════════════════════════════════════════════════════════════════════════


def outbox_event_for_action(
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id: UUID,
    aggregate_version: int = 1,
    idempotency_key: str,
    producer: str = "mneme-api",
    payload_json: dict[str, Any] | None = None,
    visibility: str = "internal",
    causation_id: UUID | None = None,
    occurred_at: datetime | None = None,
) -> OutboxEvent:
    """Build an :class:`OutboxEvent` for a domain action.

    This is the primary outbox-event factory.  It ensures every outbox event
    carries a consistent ``producer`` and ``visibility`` default while
    allowing callers to override them when needed.

    Parameters
    ----------
    event_type : str
        Reverse-DNS or dotted event name, e.g. ``"project.created"``,
        ``"memory.deleted"``, ``"agent.token_revoked"``.
    aggregate_type : str
        Logical aggregate name matching the domain resource, e.g. ``"project"``,
        ``"memory"``, ``"agent"``, ``"object"``.
    aggregate_id : UUID
        Primary key of the domain row being mutated.
    aggregate_version : int
        Monotonic version counter for this aggregate.  Default ``1`` for creates.
    idempotency_key : str
        Idempotency key from the request header.  Must be non-empty for Phase 1
        writes.
    producer : str
        Logical producer name.  Default ``"mneme-api"``.
    payload_json : dict[str, Any] | None
        Event payload.  ``request_id`` and ``correlation_id`` are automatically
        merged by :func:`add_outbox_event`, so callers should **not** include them.
    visibility : str
        One of ``"internal"``, ``"external"``, ``"audit_only"``.  Default
        ``"internal"``.
    causation_id : UUID | None
        The ``event_id`` that caused this event (for event chaining).
    occurred_at : datetime | None
        When the event logically occurred.  Defaults to ``datetime.now(timezone.utc)``
        at write time.

    Returns
    -------
    OutboxEvent
        A configured dataclass ready to pass to :func:`emit_outbox_event` or
        :func:`write_with_audit_outbox_idempotency`.
    """
    return OutboxEvent(
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        aggregate_version=aggregate_version,
        idempotency_key=idempotency_key,
        producer=producer,
        payload_json=payload_json or {},
        visibility=visibility,
        publish_state="pending",
        causation_id=causation_id,
        occurred_at=occurred_at,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Write helpers: construct + write in one call
# ═══════════════════════════════════════════════════════════════════════════════


def emit_audit_event(
    db: Session,
    context: RequestContext,
    *,
    action: str,
    result: str = "success",
    object_type: str | None = None,
    object_id: UUID | None = None,
    project_id: UUID | None = None,
    reason_code: str | None = None,
    sensitivity_level: str = "normal",
    review_item_id: UUID | None = None,
    diff_summary: dict[str, Any] | None = None,
    metadata_json: dict[str, Any] | None = None,
) -> UUID:
    """Construct an :class:`AuditEvent` and write it to ``audit_events``.

    This is the recommended single-call helper for writing audit records.
    It delegates field classification to :func:`audit_event_for_action` and
    persistence to :func:`mneme.db.audit.add_audit_event`.

    Callers that need policy-specific classification (e.g. a denied action)
    should use :func:`audit_event_for_policy_denied` together with
    :func:`mneme.db.audit.add_audit_event` directly.

    Parameters
    ----------
    db : Session
        Active SQLAlchemy session.
    context : RequestContext
        Must carry the authenticated actor (actor_type, actor_id).
    action : str
        Namespaced action name, e.g. ``"project.create"``, ``"auth.login"``.
    result : str
        One of ``"success"``, ``"denied"``, ``"failed"``.
    object_type : str | None
        Logical resource type, e.g. ``"project"``, ``"agent"``, ``"memory"``.
    object_id : UUID | None
        The affected domain object's primary key.
    project_id : UUID | None
        Owning project's primary key.
    reason_code : str | None
        Machine-readable reason (e.g. a :class:`DenyReason` value).
    sensitivity_level : str
        Sensitivity label.  Default ``"normal"``.
    review_item_id : UUID | None
        Review item id when the action triggered a review workflow.
    diff_summary : dict[str, Any] | None
        Key-value summary of what changed (for update/deletes).
    metadata_json : dict[str, Any] | None
        Arbitrary metadata attached to the audit row.

    Returns
    -------
    UUID
        The newly created ``audit_id``.
    """
    event = audit_event_for_action(
        action=action,
        result=result,
        object_type=object_type,
        object_id=object_id,
        project_id=project_id,
        reason_code=reason_code,
        sensitivity_level=sensitivity_level,
        diff_summary=diff_summary,
        metadata_json=metadata_json,
    )
    # The review_item_id is not on the base audit_event_for_action signature
    # so we set it directly on the returned event.
    if review_item_id is not None:
        event = AuditEvent(
            action=event.action,
            result=event.result,
            object_type=event.object_type,
            object_id=event.object_id,
            project_id=event.project_id,
            reason_code=event.reason_code,
            sensitivity_level=event.sensitivity_level,
            review_item_id=review_item_id,
            diff_summary=event.diff_summary,
            metadata_json=event.metadata_json,
        )
    return add_audit_event(db, context, event)


def emit_outbox_event(
    db: Session,
    context: RequestContext,
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id: UUID,
    aggregate_version: int = 1,
    idempotency_key: str,
    producer: str = "mneme-api",
    payload_json: dict[str, Any] | None = None,
    visibility: str = "internal",
    causation_id: UUID | None = None,
    occurred_at: datetime | None = None,
) -> UUID:
    """Construct an :class:`OutboxEvent` and write it to ``events``.

    This is the recommended single-call helper for publishing outbox events.
    It delegates construction to :func:`outbox_event_for_action` and
    persistence to :func:`mneme.db.audit.add_outbox_event`.

    The ``request_id`` and ``correlation_id`` from *context* are
    automatically merged into ``payload_json`` by the DB layer.

    Parameters
    ----------
    db : Session
        Active SQLAlchemy session.
    context : RequestContext
        Carries ``request_id``, ``correlation_id``, and idempotency key.
    event_type : str
        Event type name, e.g. ``"project.created"``, ``"memory.deleted"``.
    aggregate_type : str
        Logical aggregate name, e.g. ``"project"``, ``"memory"``.
    aggregate_id : UUID
        Domain object primary key.
    aggregate_version : int
        Monotonic version counter.  Default ``1`` for creates.
    idempotency_key : str
        Idempotency key from the request header.
    producer : str
        Logical producer name.  Default ``"mneme-api"``.
    payload_json : dict[str, Any] | None
        Event payload (``request_id`` / ``correlation_id`` are auto-merged).
    visibility : str
        One of ``"internal"``, ``"external"``, ``"audit_only"``.
    causation_id : UUID | None
        The ``event_id`` that caused this event.
    occurred_at : datetime | None
        When the event logically occurred.  Defaults to ``datetime.now(timezone.utc)``.

    Returns
    -------
    UUID
        The newly created ``event_id``.
    """
    event = outbox_event_for_action(
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        aggregate_version=aggregate_version,
        idempotency_key=idempotency_key,
        producer=producer,
        payload_json=payload_json,
        visibility=visibility,
        causation_id=causation_id,
        occurred_at=occurred_at,
    )
    return add_outbox_event(db, context, event)


def emit_audit_and_outbox(
    db: Session,
    context: RequestContext,
    *,
    # ── audit fields ──
    action: str,
    result: str = "success",
    object_type: str | None = None,
    object_id: UUID | None = None,
    project_id: UUID | None = None,
    reason_code: str | None = None,
    sensitivity_level: str = "normal",
    diff_summary: dict[str, Any] | None = None,
    metadata_json: dict[str, Any] | None = None,
    # ── outbox fields ──
    event_type: str,
    aggregate_type: str,
    aggregate_id: UUID,
    aggregate_version: int = 1,
    idempotency_key: str,
    producer: str = "mneme-api",
    payload_json: dict[str, Any] | None = None,
    visibility: str = "internal",
    causation_id: UUID | None = None,
    occurred_at: datetime | None = None,
) -> tuple[UUID, UUID]:
    """Construct and write both an audit event and an outbox event in one call.

    This is the simplest helper for non-idempotent writes where both audit and
    outbox records are needed.  For idempotent writes use
    :func:`write_with_audit_outbox_idempotency` instead.

    Both inserts happen in the **caller's** transaction.  It is the caller's
    responsibility to ensure they are wrapped in the same transaction as the
    business-row insert.

    Parameters
    ----------
    db : Session
        Active SQLAlchemy session.
    context : RequestContext
        Request context carrying actor and tracing ids.
    action : str
        Namespaced action name for the audit record.
    result : str
        Audit result: ``"success"``, ``"denied"``, or ``"failed"``.
    object_type : str | None
        Logical resource type.
    object_id : UUID | None
        Affected domain object id.
    project_id : UUID | None
        Owning project id.
    reason_code : str | None
        Machine-readable reason code.
    sensitivity_level : str
        Sensitivity label.  Default ``"normal"``.
    diff_summary : dict[str, Any] | None
        Summary of what changed.
    metadata_json : dict[str, Any] | None
        Additional audit metadata.
    event_type : str
        Outbox event type, e.g. ``"project.created"``.
    aggregate_type : str
        Logical aggregate name.
    aggregate_id : UUID
        Domain object primary key (same as *object_id* for typical writes).
    aggregate_version : int
        Monotonic version counter.  Default ``1``.
    idempotency_key : str
        Idempotency key from request header.
    producer : str
        Logical producer.  Default ``"mneme-api"``.
    payload_json : dict[str, Any] | None
        Event payload.
    visibility : str
        Event visibility.  Default ``"internal"``.
    causation_id : UUID | None
        Causation event id.
    occurred_at : datetime | None
        Logical occurrence time.  Defaults to now at write time.

    Returns
    -------
    tuple[UUID, UUID]
        ``(audit_id, event_id)`` of the newly inserted rows.
    """
    audit_id = emit_audit_event(
        db,
        context,
        action=action,
        result=result,
        object_type=object_type,
        object_id=object_id,
        project_id=project_id,
        reason_code=reason_code,
        sensitivity_level=sensitivity_level,
        diff_summary=diff_summary,
        metadata_json=metadata_json,
    )
    outbox_event_id = emit_outbox_event(
        db,
        context,
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        aggregate_version=aggregate_version,
        idempotency_key=idempotency_key,
        producer=producer,
        payload_json=payload_json,
        visibility=visibility,
        causation_id=causation_id,
        occurred_at=occurred_at,
    )
    return audit_id, outbox_event_id


# ═══════════════════════════════════════════════════════════════════════════════
# Re-exports: transactional write helpers from mneme.db.audit
# ═══════════════════════════════════════════════════════════════════════════════

# write_with_audit_and_outbox      – already imported and re-exported above
# write_with_audit_outbox_idempotency – already imported and re-exported above


# ── internal helpers ───────────────────────────────────────────────────────────

def _policy_metadata(decision: PolicyDecision) -> dict[str, Any]:
    """Extract a consistent metadata dict from a PolicyDecision."""
    meta: dict[str, Any] = {}
    if decision.message:
        meta["policy_message"] = decision.message
    if decision.details:
        meta["policy_details"] = decision.details
    return meta
