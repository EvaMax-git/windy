"""Conversation CRUD with audit + outbox + idempotency + object registry.

P4-01 — conversations table.

Every write mutation is wrapped in
:func:`mneme.db.audit.write_with_audit_outbox_idempotency`.
"""

from __future__ import annotations

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
    create_version,
    register_object,
)
from mneme.schemas.conversations import (
    ConversationCreateRequest,
    ConversationRead,
    ConversationUpdateRequest,
)


_INSERT_CONVERSATION = text(
    """
    INSERT INTO conversations (
      conversation_id,
      project_id,
      owner_user_id,
      conversation_type,
      title,
      source_platform,
      sensitivity_level,
      retention_days,
      started_at
    )
    VALUES (
      :conversation_id,
      :project_id,
      :owner_user_id,
      :conversation_type,
      :title,
      :source_platform,
      :sensitivity_level,
      :retention_days,
      :started_at
    )
    RETURNING
      conversation_id,
      project_id,
      owner_user_id,
      conversation_type,
      title,
      source_platform,
      sensitivity_level,
      retention_days,
      conversation_status,
      started_at,
      ended_at,
      created_at,
      updated_at
    """
).bindparams(
    bindparam("conversation_id", type_=PG_UUID(as_uuid=True)),
    bindparam("project_id", type_=PG_UUID(as_uuid=True)),
    bindparam("owner_user_id", type_=PG_UUID(as_uuid=True)),
)


_SELECT_CONVERSATION_BY_ID = text(
    """
    SELECT
      conversation_id,
      project_id,
      owner_user_id,
      conversation_type,
      title,
      source_platform,
      sensitivity_level,
      retention_days,
      conversation_status,
      started_at,
      ended_at,
      created_at,
      updated_at
    FROM conversations
    WHERE conversation_id = :conversation_id
    """
).bindparams(bindparam("conversation_id", type_=PG_UUID(as_uuid=True)))


_LIST_CONVERSATIONS_COUNT = text(
    """
    SELECT count(*) FROM conversations
    WHERE (:conversation_status IS NULL OR conversation_status = :conversation_status)
      AND (:project_id IS NULL OR project_id = :project_id)
      AND (:conversation_type IS NULL OR conversation_type = :conversation_type)
    """
).bindparams(bindparam("project_id", type_=PG_UUID(as_uuid=True)))


_LIST_CONVERSATIONS = text(
    """
    SELECT
      conversation_id,
      project_id,
      owner_user_id,
      conversation_type,
      title,
      source_platform,
      sensitivity_level,
      retention_days,
      conversation_status,
      started_at,
      ended_at,
      created_at,
      updated_at
    FROM conversations
    WHERE (:conversation_status IS NULL OR conversation_status = :conversation_status)
      AND (:project_id IS NULL OR project_id = :project_id)
      AND (:conversation_type IS NULL OR conversation_type = :conversation_type)
    ORDER BY COALESCE(started_at, created_at) DESC
    LIMIT :page_size OFFSET :offset
    """
).bindparams(bindparam("project_id", type_=PG_UUID(as_uuid=True)))


_UPDATE_CONVERSATION = text(
    """
    UPDATE conversations
    SET title = COALESCE(:title, title),
        sensitivity_level = COALESCE(:sensitivity_level, sensitivity_level),
        retention_days = COALESCE(:retention_days, retention_days)
    WHERE conversation_id = :conversation_id
      AND conversation_status = 'active'
    RETURNING
      conversation_id,
      project_id,
      owner_user_id,
      conversation_type,
      title,
      source_platform,
      sensitivity_level,
      retention_days,
      conversation_status,
      started_at,
      ended_at,
      created_at,
      updated_at
    """
).bindparams(bindparam("conversation_id", type_=PG_UUID(as_uuid=True)))


_ARCHIVE_CONVERSATION = text(
    """
    UPDATE conversations
    SET conversation_status = 'archived',
        ended_at = now()
    WHERE conversation_id = :conversation_id
      AND conversation_status = 'active'
    RETURNING
      conversation_id,
      project_id,
      owner_user_id,
      conversation_type,
      title,
      source_platform,
      sensitivity_level,
      retention_days,
      conversation_status,
      started_at,
      ended_at,
      created_at,
      updated_at
    """
).bindparams(bindparam("conversation_id", type_=PG_UUID(as_uuid=True)))


_DELETE_CONVERSATION = text(
    """
    UPDATE conversations
    SET conversation_status = 'deleted'
    WHERE conversation_id = :conversation_id
      AND conversation_status != 'deleted'
    RETURNING
      conversation_id,
      project_id,
      owner_user_id,
      conversation_type,
      title,
      source_platform,
      sensitivity_level,
      retention_days,
      conversation_status,
      started_at,
      ended_at,
      created_at,
      updated_at
    """
).bindparams(bindparam("conversation_id", type_=PG_UUID(as_uuid=True)))


_SET_STARTED_AT = text(
    """
    UPDATE conversations
    SET started_at = :started_at
    WHERE conversation_id = :conversation_id
      AND started_at IS NULL
    """
).bindparams(
    bindparam("conversation_id", type_=PG_UUID(as_uuid=True)),
)


def _conv_from_row(row: Any) -> ConversationRead:
    data = dict(row._mapping)
    return ConversationRead.model_validate(data)


# ══════════════════════════════════════════════════════════════════════
# Create
# ══════════════════════════════════════════════════════════════════════

def create_conversation(
    db: Session,
    context: RequestContext,
    *,
    payload: ConversationCreateRequest,
) -> ConversationRead:
    """Create a conversation with audit, outbox, and object registry."""
    conversation_id = uuid4()
    object_type = "conversation"
    owner_user_id = context.actor.actor_id if context.actor.actor_type == "user" else None

    outbox_event = OutboxEvent(
        event_type="conversation.created",
        aggregate_type=object_type,
        aggregate_id=conversation_id,
        aggregate_version=1,
        idempotency_key=context.idempotency_key or str(uuid4()),
        producer="mneme-api",
        payload_json={
            "conversation_id": str(conversation_id),
            "project_id": str(payload.project_id),
            "conversation_type": payload.conversation_type.value,
            "source_platform": payload.source_platform,
        },
        visibility="internal",
        publish_state="pending",
    )

    audit_event = AuditEvent(
        action="conversation.create",
        result="success",
        object_type=object_type,
        object_id=conversation_id,
        project_id=payload.project_id,
        sensitivity_level=payload.sensitivity_level.value,
    )

    def _do_insert(db2: Session) -> ConversationRead:
        row = db2.execute(
            _INSERT_CONVERSATION,
            {
                "conversation_id": conversation_id,
                "project_id": payload.project_id,
                "owner_user_id": owner_user_id,
                "conversation_type": payload.conversation_type.value,
                "title": payload.title,
                "source_platform": payload.source_platform,
                "sensitivity_level": payload.sensitivity_level.value,
                "retention_days": payload.retention_days,
                "started_at": payload.started_at,
            },
        ).one()

        register_object(
            db2,
            object_id=conversation_id,
            object_type=object_type,
            project_id=payload.project_id,
            object_key=f"conv-{conversation_id}",
            owner_actor_type=context.actor.actor_type,
            owner_actor_id=context.actor.actor_id,
            sensitivity_level=payload.sensitivity_level.value,
        )

        return _conv_from_row(row)

    def _post_audit(db2: Session, audit_id: UUID, event_id: UUID) -> None:
        create_version(
            db2,
            object_id=conversation_id,
            object_type=object_type,
            version=1,
            action="create",
            actor_type=context.actor.actor_type,
            actor_id=context.actor.actor_id,
            audit_id=audit_id,
            event_id=event_id,
        )

    def _resolve_existing(db2: Session, aggregate_id: UUID) -> ConversationRead:
        row = db2.execute(_SELECT_CONVERSATION_BY_ID, {"conversation_id": aggregate_id}).first()
        if row is None:
            raise LookupError(f"conversation {aggregate_id} not found during idempotent replay")
        return _conv_from_row(row)

    return write_with_audit_outbox_idempotency(
        db,
        context,
        work=_do_insert,
        audit_event=audit_event,
        outbox_event=outbox_event,
        resolve_existing=_resolve_existing,
        on_success=_post_audit,
    )


# ══════════════════════════════════════════════════════════════════════
# Read
# ══════════════════════════════════════════════════════════════════════

def get_conversation(db: Session, conversation_id: UUID) -> ConversationRead | None:
    """Look up a conversation by primary key."""
    row = db.execute(_SELECT_CONVERSATION_BY_ID, {"conversation_id": conversation_id}).first()
    if row is None:
        return None
    return _conv_from_row(row)


def list_conversations(
    db: Session,
    *,
    project_id: UUID | None = None,
    conversation_type: str | None = None,
    conversation_status: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[ConversationRead], int]:
    """List conversations with optional filters and pagination."""
    params = {
        "project_id": project_id,
        "conversation_type": conversation_type,
        "conversation_status": conversation_status,
    }
    total = db.execute(_LIST_CONVERSATIONS_COUNT, params).scalar_one()
    offset = (page - 1) * page_size
    rows = db.execute(
        _LIST_CONVERSATIONS,
        {**params, "page_size": page_size, "offset": offset},
    ).all()
    items = [_conv_from_row(row) for row in rows]
    return items, total


# ══════════════════════════════════════════════════════════════════════
# Update
# ══════════════════════════════════════════════════════════════════════

def update_conversation(
    db: Session,
    context: RequestContext,
    *,
    conversation_id: UUID,
    payload: ConversationUpdateRequest,
) -> ConversationRead:
    """Update conversation mutable fields (title, sensitivity_level, retention_days)."""
    conv = get_conversation(db, conversation_id)
    if conv is None:
        raise ValueError(f"conversation {conversation_id} not found")
    if conv.conversation_status != "active":
        raise ValueError(f"conversation {conversation_id} is not active (status={conv.conversation_status})")

    object_type = "conversation"

    outbox_event = OutboxEvent(
        event_type="conversation.updated",
        aggregate_type=object_type,
        aggregate_id=conversation_id,
        aggregate_version=1,
        idempotency_key=context.idempotency_key or str(uuid4()),
        producer="mneme-api",
        payload_json={
            "conversation_id": str(conversation_id),
            "changed_fields": payload.model_dump(exclude_none=True, mode="json"),
        },
        visibility="internal",
        publish_state="pending",
    )

    audit_event = AuditEvent(
        action="conversation.update",
        result="success",
        object_type=object_type,
        object_id=conversation_id,
        project_id=conv.project_id,
        sensitivity_level=conv.sensitivity_level,
    )

    def _do_update(db2: Session) -> ConversationRead:
        row = db2.execute(
            _UPDATE_CONVERSATION,
            {
                "conversation_id": conversation_id,
                "title": payload.title,
                "sensitivity_level": payload.sensitivity_level.value if payload.sensitivity_level else None,
                "retention_days": payload.retention_days,
            },
        ).first()
        if row is None:
            raise ValueError(f"conversation {conversation_id} disappeared during update")
        return _conv_from_row(row)

    def _resolve_existing(db2: Session, aggregate_id: UUID) -> ConversationRead:
        return _conv_from_row(
            db2.execute(_SELECT_CONVERSATION_BY_ID, {"conversation_id": aggregate_id}).first()
        )

    return write_with_audit_outbox_idempotency(
        db,
        context,
        work=_do_update,
        audit_event=audit_event,
        outbox_event=outbox_event,
        resolve_existing=_resolve_existing,
    )


# ══════════════════════════════════════════════════════════════════════
# Archive / Delete
# ══════════════════════════════════════════════════════════════════════

def archive_conversation(
    db: Session,
    context: RequestContext,
    *,
    conversation_id: UUID,
) -> ConversationRead:
    """Archive a conversation (active → archived). Sets ended_at."""
    conv = get_conversation(db, conversation_id)
    if conv is None:
        raise ValueError(f"conversation {conversation_id} not found")
    if conv.conversation_status != "active":
        raise ValueError(f"conversation {conversation_id} is not active (status={conv.conversation_status})")

    object_type = "conversation"

    outbox_event = OutboxEvent(
        event_type="conversation.archived",
        aggregate_type=object_type,
        aggregate_id=conversation_id,
        aggregate_version=1,
        idempotency_key=context.idempotency_key or str(uuid4()),
        producer="mneme-api",
        payload_json={"conversation_id": str(conversation_id)},
        visibility="internal",
        publish_state="pending",
    )

    audit_event = AuditEvent(
        action="conversation.archive",
        result="success",
        object_type=object_type,
        object_id=conversation_id,
        project_id=conv.project_id,
        sensitivity_level=conv.sensitivity_level,
    )

    def _do_archive(db2: Session) -> ConversationRead:
        row = db2.execute(_ARCHIVE_CONVERSATION, {"conversation_id": conversation_id}).first()
        if row is None:
            raise ValueError(f"conversation {conversation_id} cannot be archived")
        return _conv_from_row(row)

    def _resolve_existing(db2: Session, aggregate_id: UUID) -> ConversationRead:
        return _conv_from_row(
            db2.execute(_SELECT_CONVERSATION_BY_ID, {"conversation_id": aggregate_id}).first()
        )

    return write_with_audit_outbox_idempotency(
        db,
        context,
        work=_do_archive,
        audit_event=audit_event,
        outbox_event=outbox_event,
        resolve_existing=_resolve_existing,
    )


def delete_conversation(
    db: Session,
    context: RequestContext,
    *,
    conversation_id: UUID,
) -> ConversationRead:
    """Soft-delete a conversation (status → 'deleted')."""
    conv = get_conversation(db, conversation_id)
    if conv is None:
        raise ValueError(f"conversation {conversation_id} not found")

    object_type = "conversation"

    outbox_event = OutboxEvent(
        event_type="conversation.deleted",
        aggregate_type=object_type,
        aggregate_id=conversation_id,
        aggregate_version=1,
        idempotency_key=context.idempotency_key or str(uuid4()),
        producer="mneme-api",
        payload_json={"conversation_id": str(conversation_id)},
        visibility="internal",
        publish_state="pending",
    )

    audit_event = AuditEvent(
        action="conversation.delete",
        result="success",
        object_type=object_type,
        object_id=conversation_id,
        project_id=conv.project_id,
        sensitivity_level=conv.sensitivity_level,
    )

    def _do_delete(db2: Session) -> ConversationRead:
        row = db2.execute(_DELETE_CONVERSATION, {"conversation_id": conversation_id}).first()
        if row is None:
            raise ValueError(f"conversation {conversation_id} not found or already deleted")
        return _conv_from_row(row)

    def _resolve_existing(db2: Session, aggregate_id: UUID) -> ConversationRead:
        return _conv_from_row(
            db2.execute(_SELECT_CONVERSATION_BY_ID, {"conversation_id": aggregate_id}).first()
        )

    return write_with_audit_outbox_idempotency(
        db,
        context,
        work=_do_delete,
        audit_event=audit_event,
        outbox_event=outbox_event,
        resolve_existing=_resolve_existing,
    )


# ══════════════════════════════════════════════════════════════════════
# Internal helper (called by messages module)
# ══════════════════════════════════════════════════════════════════════

def ensure_conversation_started_at(
    db: Session,
    *,
    conversation_id: UUID,
    message_time: datetime,
) -> bool:
    """Set conversation.started_at if it's NULL (called on first message write).

    Returns True if started_at was set, False if it was already set.
    """
    result = db.execute(
        _SET_STARTED_AT,
        {
            "conversation_id": conversation_id,
            "started_at": message_time,
        },
    )
    return result.rowcount > 0

