"""Inbox item CRUD with audit + outbox + idempotency + object registry.

Every mutation that creates or modifies an ``inbox_items`` row is wrapped in
:func:`mneme.db.audit.write_with_audit_outbox_idempotency` so that:

* A ``inbox_items`` row is written.
* An ``object_registry`` row is written (P1-09).
* An ``object_versions`` row is written (with ``action='create'`` or ``action='update'``).
* An ``audit_events`` row is written.
* An ``events`` (outbox) row is written.
* All five land in the same database transaction.

Status machine
--------------
``received → staged → linked → processed`` (or ``rejected`` / ``failed`` / ``archived``)

* ``received`` — initial state for non-file inbox items (url, text, etc.)
* ``staged`` — file has been staged to disk (for file types) or item ready for processing
* ``linked`` — item has been linked to an asset (via ``asset_id`` FK)
* ``processed`` — item fully processed by pipeline, ``processed_at`` set to ``now()``
* ``rejected`` — item was rejected (invalid format, policy, etc.)
* ``failed`` — processing failed
* ``archived`` — soft-archived
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session

from mneme.api.context import RequestContext
from mneme.db.audit import (
    AuditEvent,
    OutboxEvent,
    write_with_audit_outbox_idempotency,
)
from mneme.domain.objects import (
    bump_version,
    create_version,
    get_registry,
    register_object,
)
from mneme.schemas.storage import (
    InboxItemCreateRequest,
    InboxItemRead,
    InboxStatus,
    StagedFileInfo,
)


# ═══════════════════════════════════════════════════════════════════
# SQL statements
# ═══════════════════════════════════════════════════════════════════

_INSERT_INBOX = text(
    """
    INSERT INTO inbox_items (
      inbox_item_id,
      project_id,
      inbox_type,
      source,
      source_uri,
      source_ref,
      status,
      title,
      content_hash,
      payload_json,
      metadata_json,
      created_by_actor_type,
      created_by_actor_id,
      received_at
    )
    VALUES (
      :inbox_item_id,
      :project_id,
      :inbox_type,
      :source,
      :source_uri,
      :source_ref,
      :status,
      :title,
      :content_hash,
      :payload_json,
      :metadata_json,
      :created_by_actor_type,
      :created_by_actor_id,
      :received_at
    )
    RETURNING
      inbox_item_id,
      project_id,
      inbox_type,
      source,
      source_uri,
      source_ref,
      status,
      asset_id,
      title,
      content_hash,
      received_at,
      processed_at,
      created_at,
      updated_at
    """
).bindparams(
    bindparam("inbox_item_id", type_=PG_UUID(as_uuid=True)),
    bindparam("project_id", type_=PG_UUID(as_uuid=True)),
)

_SELECT_INBOX_BY_ID = text(
    """
    SELECT
      inbox_item_id,
      project_id,
      inbox_type,
      source,
      source_uri,
      source_ref,
      status,
      asset_id,
      title,
      content_hash,
      received_at,
      processed_at,
      created_at,
      updated_at
    FROM inbox_items
    WHERE inbox_item_id = :inbox_item_id
    """
).bindparams(bindparam("inbox_item_id", type_=PG_UUID(as_uuid=True)))

_LIST_INBOX_COUNT = text(
    """
    SELECT count(*) FROM inbox_items
    WHERE (:project_id IS NULL OR project_id = :project_id)
      AND (:status IS NULL OR status = :status)
    """
).bindparams(bindparam("project_id", type_=PG_UUID(as_uuid=True)))

_LIST_INBOX = text(
    """
    SELECT
      inbox_item_id,
      project_id,
      inbox_type,
      source,
      source_uri,
      source_ref,
      status,
      asset_id,
      title,
      content_hash,
      received_at,
      processed_at,
      created_at,
      updated_at
    FROM inbox_items
    WHERE (:project_id IS NULL OR project_id = :project_id)
      AND (:status IS NULL OR status = :status)
    ORDER BY received_at DESC
    LIMIT :page_size OFFSET :offset
    """
).bindparams(bindparam("project_id", type_=PG_UUID(as_uuid=True)))

_UPDATE_INBOX_STATUS = text(
    """
    UPDATE inbox_items
    SET status = :new_status,
        asset_id = COALESCE(:asset_id, asset_id),
        processed_at = :processed_at,
        updated_at = now()
    WHERE inbox_item_id = :inbox_item_id
      AND status = :expected_status
    RETURNING
      inbox_item_id,
      project_id,
      inbox_type,
      source,
      source_uri,
      source_ref,
      status,
      asset_id,
      title,
      content_hash,
      received_at,
      processed_at,
      created_at,
      updated_at
    """
).bindparams(
    bindparam("inbox_item_id", type_=PG_UUID(as_uuid=True)),
    bindparam("asset_id", type_=PG_UUID(as_uuid=True)),
)

_LOOKUP_INBOX_BY_HASH = text(
    """
    SELECT
      inbox_item_id,
      project_id,
      inbox_type,
      source,
      source_uri,
      source_ref,
      status,
      asset_id,
      title,
      content_hash,
      received_at,
      processed_at,
      created_at,
      updated_at
    FROM inbox_items
    WHERE content_hash = :content_hash
      AND project_id = :project_id
      AND status != 'rejected'
    ORDER BY received_at DESC
    LIMIT 1
    """
).bindparams(bindparam("project_id", type_=PG_UUID(as_uuid=True)))


# ═══════════════════════════════════════════════════════════════════
# Row mapping
# ═══════════════════════════════════════════════════════════════════

def _inbox_from_row(row: Any) -> InboxItemRead:
    data = dict(row._mapping)
    return InboxItemRead.model_validate(data)


def _idempotent_resolve(db: Session, inbox_item_id: UUID) -> InboxItemRead:
    """Resolve an existing inbox item by id (used in idempotent replay)."""
    row = db.execute(_SELECT_INBOX_BY_ID, {"inbox_item_id": inbox_item_id}).first()
    if row is None:
        raise LookupError(f"inbox_item {inbox_item_id} not found during idempotent replay")
    return _inbox_from_row(row)


# ═══════════════════════════════════════════════════════════════════
# Valid transitions
# ═══════════════════════════════════════════════════════════════════

_VALID_TRANSITIONS: dict[str, set[str]] = {
    "received": {"staged", "rejected", "failed", "archived"},
    "staged": {"linked", "rejected", "failed", "archived"},
    "linked": {"processed", "rejected", "failed", "archived"},
    "processed": {"archived"},
    "rejected": {"received", "archived"},
    "failed": {"received", "archived"},
    "archived": {"received"},
}


def _can_transition(current: str, target: str) -> bool:
    """Check if *current → target* is a valid status transition."""
    return target in _VALID_TRANSITIONS.get(current, set())


# ═══════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════


def create_inbox_item(
    db: Session,
    context: RequestContext,
    *,
    payload: InboxItemCreateRequest,
    status: str = "received",
) -> InboxItemRead:
    """Create an inbox item with audit, outbox, idempotency, and object registry.

    Args:
        db: Active SQLAlchemy session.
        context: Request context (must carry idempotency_key).
        payload: Inbox item creation data.
        status: Initial status (``"received"`` for non-file types,
                ``"staged"`` for file uploads).

    Returns:
        The newly created :class:`InboxItemRead` (or the previously created
        one if the idempotency key was already used).
    """
    inbox_item_id = uuid4()
    object_type = "inbox_item"

    outbox_event = OutboxEvent(
        event_type="inbox_item.created",
        aggregate_type=object_type,
        aggregate_id=inbox_item_id,
        aggregate_version=1,
        idempotency_key=context.idempotency_key or "",
        producer="mneme-api",
        payload_json={
            "project_id": str(payload.project_id),
            "inbox_type": payload.inbox_type.value if hasattr(payload.inbox_type, 'value') else payload.inbox_type,
            "status": status,
            "title": payload.title,
        },
        visibility="internal",
        publish_state="pending",
    )

    audit_event = AuditEvent(
        action="inbox_item.create",
        result="success",
        object_type=object_type,
        object_id=inbox_item_id,
        project_id=payload.project_id,
        sensitivity_level="normal",
    )

    def _do_insert(db: Session) -> InboxItemRead:
        inbox_type_val = payload.inbox_type.value if hasattr(payload.inbox_type, 'value') else payload.inbox_type

        row = db.execute(
            _INSERT_INBOX,
            {
                "inbox_item_id": inbox_item_id,
                "project_id": payload.project_id,
                "inbox_type": inbox_type_val,
                "source": payload.source,
                "source_uri": payload.source_uri,
                "source_ref": payload.source_ref,
                "status": status,
                "title": payload.title,
                "content_hash": payload.content_hash,
                "payload_json": json.dumps(payload.payload_json or {}),
                "metadata_json": json.dumps(payload.metadata_json or {}),
                "created_by_actor_type": context.actor.actor_type,
                "created_by_actor_id": context.actor.actor_id,
                "received_at": datetime.now(timezone.utc),
            },
        ).one()

        # Register in object_registry
        register_object(
            db,
            object_id=inbox_item_id,
            object_type=object_type,
            project_id=payload.project_id,
            object_key=f"inbox:{payload.source}:{inbox_item_id}",
            owner_actor_type=context.actor.actor_type,
            owner_actor_id=context.actor.actor_id,
            sensitivity_level="normal",
        )

        return _inbox_from_row(row)

    def _post_audit(db: Session, audit_id: UUID, event_id: UUID) -> None:
        create_version(
            db,
            object_id=inbox_item_id,
            object_type=object_type,
            version=1,
            action="create",
            actor_type=context.actor.actor_type,
            actor_id=context.actor.actor_id,
            audit_id=audit_id,
            event_id=event_id,
        )

    return write_with_audit_outbox_idempotency(
        db,
        context,
        work=_do_insert,
        audit_event=audit_event,
        outbox_event=outbox_event,
        resolve_existing=_idempotent_resolve,
        on_success=_post_audit,
    )


def create_inbox_from_staging(
    db: Session,
    context: RequestContext,
    *,
    project_id: UUID,
    staged_info: StagedFileInfo,
    title: str | None = None,
    source: str = "api",
    source_uri: str | None = None,
    source_ref: str | None = None,
) -> InboxItemRead:
    """Create an inbox item from an already-staged file.

    This is the preferred path for file uploads: the storage layer has already
    staged the file and computed the content hash, and this function creates the
    corresponding ``inbox_items`` row with ``status='staged'``.

    Args:
        db: Active SQLAlchemy session.
        context: Request context.
        project_id: Target project UUID.
        staged_info: Metadata from the staging operation.
        title: Optional title (defaults to original filename).
        source: Source identifier (default ``"api"``).
        source_uri: Optional source URI.
        source_ref: Optional external reference.

    Returns:
        The newly created :class:`InboxItemRead`.
    """
    payload = InboxItemCreateRequest(
        project_id=project_id,
        inbox_type="file",
        source=source,
        source_uri=source_uri or f"file://{staged_info.staging_path}",
        source_ref=source_ref,
        title=title or staged_info.original_filename,
        content_hash=staged_info.content_hash,
        payload_json={
            "staging_path": staged_info.staging_path,
            "staging_token": staged_info.staging_token,
            "size_bytes": staged_info.size_bytes,
            "media_type": staged_info.media_type,
        },
        metadata_json={},
    )

    return create_inbox_item(db, context, payload=payload, status="staged")


def get_inbox_item(db: Session, inbox_item_id: UUID) -> InboxItemRead | None:
    """Look up an inbox item by primary key."""
    row = db.execute(_SELECT_INBOX_BY_ID, {"inbox_item_id": inbox_item_id}).first()
    if row is None:
        return None
    return _inbox_from_row(row)


def list_inbox_items(
    db: Session,
    *,
    project_id: UUID | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[InboxItemRead], int]:
    """List inbox items with optional filters and pagination.

    Args:
        db: Active session.
        project_id: Optional project filter.
        status: Optional status filter (one of ``InboxStatus`` values).
        page: Page number (1-based).
        page_size: Items per page.

    Returns:
        ``(items, total_count)`` tuple.
    """
    total = db.execute(
        _LIST_INBOX_COUNT,
        {"project_id": project_id, "status": status},
    ).scalar_one()
    offset = (page - 1) * page_size
    rows = db.execute(
        _LIST_INBOX,
        {
            "project_id": project_id,
            "status": status,
            "page_size": page_size,
            "offset": offset,
        },
    ).all()
    items = [_inbox_from_row(row) for row in rows]
    return items, total


def lookup_inbox_by_hash(
    db: Session,
    *,
    content_hash: str,
    project_id: UUID,
) -> InboxItemRead | None:
    """Find an existing inbox item with the same content hash in the same project.

    Used for idempotent upload — if a file with the same hash has already been
    uploaded, the caller can skip staging and return the existing item.
    """
    row = db.execute(
        _LOOKUP_INBOX_BY_HASH,
        {"content_hash": content_hash, "project_id": project_id},
    ).first()
    if row is None:
        return None
    return _inbox_from_row(row)


def update_inbox_status(
    db: Session,
    context: RequestContext,
    *,
    inbox_item_id: UUID,
    new_status: str,
    asset_id: UUID | None = None,
    expected_status: str | None = None,
) -> InboxItemRead:
    """Advance an inbox item to a new status, within a valid transition.

    Performs an atomic ``UPDATE ... WHERE status = :expected_status`` to
    prevent concurrent status races.  If *expected_status* is ``None``,
    the current status is read first and validated.

    The update is wrapped in ``write_with_audit_outbox_idempotency`` for
    audit trail.

    Args:
        db: Active session.
        context: Request context.
        inbox_item_id: The inbox item to update.
        new_status: Target status (must be a valid transition).
        asset_id: Optional asset to link (used for ``staged → linked``).
        expected_status: If set, the update only succeeds if the current
            status matches this value (pessimistic guard).

    Returns:
        The updated :class:`InboxItemRead`.

    Raises:
        ValueError: If the transition is invalid or the current status
            does not match *expected_status*.
    """
    current = get_inbox_item(db, inbox_item_id)
    if current is None:
        raise ValueError(f"inbox_item {inbox_item_id} not found")

    current_status = current.status.value if hasattr(current.status, 'value') else current.status

    if expected_status is not None and current_status != expected_status:
        raise ValueError(
            f"Expected status '{expected_status}' but current is '{current_status}'"
        )

    if not _can_transition(current_status, new_status):
        raise ValueError(
            f"Invalid status transition: '{current_status}' -> '{new_status}'"
        )

    # Get current version from object_registry
    object_type = "inbox_item"
    registry = get_registry(db, object_id=inbox_item_id, object_type=object_type)
    if registry is None:
        raise ValueError(f"object_registry entry not found for inbox_item {inbox_item_id}")
    current_version = registry.current_version
    next_version = current_version + 1

    # Set processed_at when entering 'processed'
    processed_at = datetime.now(timezone.utc) if new_status == "processed" else None

    outbox_event = OutboxEvent(
        event_type=f"inbox_item.{new_status}",
        aggregate_type=object_type,
        aggregate_id=inbox_item_id,
        aggregate_version=next_version,
        idempotency_key=f"{context.idempotency_key or ''}:status:{new_status}",
        producer="mneme-api",
        payload_json={
            "previous_status": current_status,
            "new_status": new_status,
            "asset_id": str(asset_id) if asset_id else None,
        },
        visibility="internal",
        publish_state="pending",
    )

    audit_event = AuditEvent(
        action="inbox_item.update_status",
        result="success",
        object_type=object_type,
        object_id=inbox_item_id,
        project_id=current.project_id,
        sensitivity_level="normal",
        diff_summary={
            "status": {"from": current_status, "to": new_status},
        },
    )

    def _do_update(db: Session) -> InboxItemRead:
        row = db.execute(
            _UPDATE_INBOX_STATUS,
            {
                "inbox_item_id": inbox_item_id,
                "new_status": new_status,
                "asset_id": asset_id,
                "processed_at": processed_at,
                "expected_status": current_status,
            },
        ).first()
        if row is None:
            raise ValueError(
                f"Status update conflict for inbox_item {inbox_item_id}: "
                f"expected '{current_status}' but status changed concurrently"
            )

        # Bump object_registry.current_version in the same transaction
        bump_version(
            db,
            object_id=inbox_item_id,
            object_type=object_type,
            new_version=next_version,
        )

        return _inbox_from_row(row)

    def _post_audit(db: Session, audit_id: UUID, event_id: UUID) -> None:
        create_version(
            db,
            object_id=inbox_item_id,
            object_type=object_type,
            version=next_version,
            action="update",
            actor_type=context.actor.actor_type,
            actor_id=context.actor.actor_id,
            audit_id=audit_id,
            event_id=event_id,
        )

    def _resolve_existing(_db: Session, _aggregate_id: UUID) -> InboxItemRead:
        item = get_inbox_item(_db, inbox_item_id)
        if item is None:
            raise LookupError(f"inbox_item {inbox_item_id} not found")
        return item

    return write_with_audit_outbox_idempotency(
        db,
        context,
        work=_do_update,
        audit_event=audit_event,
        outbox_event=outbox_event,
        resolve_existing=_resolve_existing,
        on_success=_post_audit,
    )


def link_inbox_to_asset(
    db: Session,
    context: RequestContext,
    *,
    inbox_item_id: UUID,
    asset_id: UUID,
) -> InboxItemRead:
    """Link an inbox item to an asset (``staged -> linked`` transition).

    This is a convenience wrapper around :func:`update_inbox_status`.

    Args:
        db: Active session.
        context: Request context.
        inbox_item_id: The inbox item to link.
        asset_id: The UUID of the newly created asset.

    Returns:
        The updated :class:`InboxItemRead` with ``status='linked'``.
    """
    return update_inbox_status(
        db,
        context,
        inbox_item_id=inbox_item_id,
        new_status="linked",
        asset_id=asset_id,
        expected_status="staged",
    )


def mark_inbox_processed(
    db: Session,
    context: RequestContext,
    *,
    inbox_item_id: UUID,
) -> InboxItemRead:
    """Mark an inbox item as processed (``linked -> processed`` transition).

    Sets ``processed_at = now()``.

    Args:
        db: Active session.
        context: Request context.
        inbox_item_id: The inbox item to mark as processed.

    Returns:
        The updated :class:`InboxItemRead`.
    """
    return update_inbox_status(
        db,
        context,
        inbox_item_id=inbox_item_id,
        new_status="processed",
        expected_status="linked",
    )
