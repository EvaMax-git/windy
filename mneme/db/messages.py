"""Messages CRUD with audit + outbox + idempotency.

P4-02 — messages table.

Key features:
* SHA-256 ``content_hash`` is automatically computed from ``content_text``.
* First message in a conversation automatically sets ``conversation.started_at``.
* Messages are immutable — no PATCH or DELETE permitted.
* Batch import supports up to 500 messages in a single transaction.
* ``UNIQUE(event_source_id, content_hash, message_time)`` ensures dedup.
"""

from __future__ import annotations

import hashlib
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
from mneme.db.conversations import ensure_conversation_started_at
from mneme.schemas.conversations import (
    BatchImportResult,
    MessageCreate,
    MessageRead,
)


# ══════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════

def compute_content_hash(content_text: str) -> str:
    """Compute SHA-256 hash of content_text for dedup and integrity."""
    return hashlib.sha256(content_text.encode("utf-8")).hexdigest()


# ══════════════════════════════════════════════════════════════════════
# SQL templates
# ══════════════════════════════════════════════════════════════════════

_INSERT_MESSAGE = text(
    """
    INSERT INTO messages (
      message_id,
      conversation_id,
      event_source_id,
      parent_message_id,
      role_code,
      sender_label,
      content_text,
      content_markdown,
      content_hash,
      sensitivity_level,
      pii_flags,
      message_time,
      ingested_at
    )
    VALUES (
      :message_id,
      :conversation_id,
      :event_source_id,
      :parent_message_id,
      :role_code,
      :sender_label,
      :content_text,
      :content_markdown,
      :content_hash,
      :sensitivity_level,
      :pii_flags,
      :message_time,
      :ingested_at
    )
    RETURNING
      message_id,
      conversation_id,
      event_source_id,
      parent_message_id,
      role_code,
      sender_label,
      content_text,
      content_markdown,
      content_hash,
      sensitivity_level,
      pii_flags,
      message_time,
      ingested_at,
      created_at,
      updated_at
    """
).bindparams(
    bindparam("message_id", type_=PG_UUID(as_uuid=True)),
    bindparam("conversation_id", type_=PG_UUID(as_uuid=True)),
    bindparam("event_source_id", type_=PG_UUID(as_uuid=True)),
    bindparam("parent_message_id", type_=PG_UUID(as_uuid=True)),
)


_SELECT_MESSAGE_BY_ID = text(
    """
    SELECT
      message_id,
      conversation_id,
      event_source_id,
      parent_message_id,
      role_code,
      sender_label,
      content_text,
      content_markdown,
      content_hash,
      sensitivity_level,
      pii_flags,
      message_time,
      ingested_at,
      created_at,
      updated_at
    FROM messages
    WHERE message_id = :message_id
    """
).bindparams(bindparam("message_id", type_=PG_UUID(as_uuid=True)))


_SELECT_MESSAGE_BY_DEDUP = text(
    """
    SELECT
      message_id,
      conversation_id,
      event_source_id,
      parent_message_id,
      role_code,
      sender_label,
      content_text,
      content_markdown,
      content_hash,
      sensitivity_level,
      pii_flags,
      message_time,
      ingested_at,
      created_at,
      updated_at
    FROM messages
    WHERE (:event_source_id IS NULL OR event_source_id = :event_source_id)
      AND content_hash = :content_hash
      AND message_time = :message_time
    LIMIT 1
    """
)


_LIST_MESSAGES_COUNT = text(
    """
    SELECT count(*) FROM messages
    WHERE conversation_id = :conversation_id
    """
).bindparams(bindparam("conversation_id", type_=PG_UUID(as_uuid=True)))


_LIST_MESSAGES = text(
    """
    SELECT
      message_id,
      conversation_id,
      event_source_id,
      parent_message_id,
      role_code,
      sender_label,
      content_text,
      content_markdown,
      content_hash,
      sensitivity_level,
      pii_flags,
      message_time,
      ingested_at,
      created_at,
      updated_at
    FROM messages
    WHERE conversation_id = :conversation_id
    ORDER BY message_time ASC, created_at ASC
    LIMIT :page_size OFFSET :offset
    """
).bindparams(bindparam("conversation_id", type_=PG_UUID(as_uuid=True)))


def _msg_from_row(row: Any) -> MessageRead:
    data = dict(row._mapping)
    # pii_flags from PostgreSQL JSONB comes as list or string
    if isinstance(data.get("pii_flags"), str):
        data["pii_flags"] = json.loads(data["pii_flags"])
    elif data.get("pii_flags") is None:
        data["pii_flags"] = []
    return MessageRead.model_validate(data)


# ══════════════════════════════════════════════════════════════════════
# Create single message
# ══════════════════════════════════════════════════════════════════════

def create_message(
    db: Session,
    context: RequestContext,
    *,
    conversation_id: UUID,
    payload: MessageCreate,
    project_id: UUID | None = None,
) -> MessageRead:
    """Write a single message into a conversation.

    * ``content_hash`` is auto-computed from ``content_text`` (SHA-256).
    * ``pii_flags`` defaults to ``[]``.
    * If the conversation has no ``started_at``, it is set to ``message_time``.
    * Messages are immutable — once written, they cannot be modified.
    * Dedup via ``UNIQUE(event_source_id, content_hash, message_time)``.
    """
    message_id = uuid4()
    content_hash = compute_content_hash(payload.content_text)
    object_type = "message"

    # Check dedup: if same event_source_id + content_hash + message_time exists, return it
    existing = _find_duplicate(
        db,
        event_source_id=payload.event_source_id,
        content_hash=content_hash,
        message_time=payload.message_time,
    )
    if existing is not None:
        return existing

    outbox_event = OutboxEvent(
        event_type="message.created",
        aggregate_type=object_type,
        aggregate_id=message_id,
        aggregate_version=1,
        idempotency_key=context.idempotency_key or "",
        producer="mneme-api",
        payload_json={
            "message_id": str(message_id),
            "conversation_id": str(conversation_id),
            "role_code": payload.role_code.value,
            "content_hash": content_hash,
        },
        visibility="internal",
        publish_state="pending",
    )

    audit_event = AuditEvent(
        action="message.create",
        result="success",
        object_type=object_type,
        object_id=message_id,
        project_id=project_id,
        sensitivity_level=payload.sensitivity_level.value,
    )

    def _do_insert(db2: Session) -> MessageRead:
        # Ensure conversation.started_at is set if this is the first message
        ensure_conversation_started_at(
            db2,
            conversation_id=conversation_id,
            message_time=payload.message_time,
        )

        row = db2.execute(
            _INSERT_MESSAGE,
            {
                "message_id": message_id,
                "conversation_id": conversation_id,
                "event_source_id": payload.event_source_id,
                "parent_message_id": payload.parent_message_id,
                "role_code": payload.role_code.value,
                "sender_label": payload.sender_label,
                "content_text": payload.content_text,
                "content_markdown": payload.content_markdown,
                "content_hash": content_hash,
                "sensitivity_level": payload.sensitivity_level.value,
                "pii_flags": json.dumps([]),
                "message_time": payload.message_time,
                "ingested_at": datetime.now(timezone.utc),
            },
        ).one()
        return _msg_from_row(row)

    def _resolve_existing(db2: Session, aggregate_id: UUID) -> MessageRead:
        row = db2.execute(_SELECT_MESSAGE_BY_ID, {"message_id": aggregate_id}).first()
        if row is None:
            raise LookupError(f"message {aggregate_id} not found during idempotent replay")
        return _msg_from_row(row)

    return write_with_audit_outbox_idempotency(
        db,
        context,
        work=_do_insert,
        audit_event=audit_event,
        outbox_event=outbox_event,
        resolve_existing=_resolve_existing,
    )


# ══════════════════════════════════════════════════════════════════════
# Batch import
# ══════════════════════════════════════════════════════════════════════

def create_message_batch(
    db: Session,
    context: RequestContext,
    *,
    conversation_id: UUID,
    messages: list[MessageCreate],
    batch_event_source_id: UUID | None = None,
    project_id: UUID | None = None,
) -> BatchImportResult:
    """Batch-import up to 500 messages in a single transaction.

    * Each message gets its own ``content_hash`` auto-computed.
    * All messages land in the same transaction — partial failure = full rollback.
    * Dedup per message: duplicate ``(event_source_id, content_hash, message_time)``
      combos are skipped (not inserted).
    * Conversation ``started_at`` is set from the earliest message_time if NULL.
    """
    object_type = "message"
    batch_id = uuid4()
    inserted_ids: list[UUID] = []
    skipped_count = 0
    earliest_time: datetime | None = None

    outbox_event = OutboxEvent(
        event_type="message.batch_created",
        aggregate_type=object_type,
        aggregate_id=batch_id,
        aggregate_version=1,
        idempotency_key=context.idempotency_key or "",
        producer="mneme-api",
        payload_json={
            "batch_id": str(batch_id),
            "conversation_id": str(conversation_id),
            "total_requested": len(messages),
        },
        visibility="internal",
        publish_state="pending",
    )

    audit_event = AuditEvent(
        action="message.batch_create",
        result="success",
        object_type=object_type,
        object_id=batch_id,
        project_id=project_id,
        sensitivity_level="private",
        diff_summary={"total_requested": len(messages)},
    )

    def _do_batch_insert(db2: Session) -> BatchImportResult:
        nonlocal inserted_ids, skipped_count, earliest_time

        # Determine earliest message_time for started_at
        times = [m.message_time for m in messages if m.message_time]
        if times:
            earliest_time = min(times)
            ensure_conversation_started_at(
                db2,
                conversation_id=conversation_id,
                message_time=earliest_time,
            )

        last_time: datetime | None = None

        for msg in messages:
            content_hash = compute_content_hash(msg.content_text)
            es_id = msg.event_source_id or batch_event_source_id

            # Dedup check within transaction
            existing = _find_duplicate(
                db2,
                event_source_id=es_id,
                content_hash=content_hash,
                message_time=msg.message_time,
            )
            if existing is not None:
                skipped_count += 1
                continue

            msg_id = uuid4()
            row = db2.execute(
                _INSERT_MESSAGE,
                {
                    "message_id": msg_id,
                    "conversation_id": conversation_id,
                    "event_source_id": es_id,
                    "parent_message_id": msg.parent_message_id,
                    "role_code": msg.role_code.value,
                    "sender_label": msg.sender_label,
                    "content_text": msg.content_text,
                    "content_markdown": msg.content_markdown,
                    "content_hash": content_hash,
                    "sensitivity_level": msg.sensitivity_level.value,
                    "pii_flags": json.dumps([]),
                    "message_time": msg.message_time,
                    "ingested_at": datetime.now(timezone.utc),
                },
            ).one()
            inserted_ids.append(msg_id)
            last_time = msg.message_time

        return BatchImportResult(
            imported_count=len(inserted_ids),
            skipped_duplicates=skipped_count,
            message_ids=inserted_ids,
            first_message_time=earliest_time,
            last_message_time=last_time,
        )

    def _resolve_existing(db2: Session, aggregate_id: UUID) -> BatchImportResult:
        # For idempotent replay of a batch, return empty result
        return BatchImportResult(
            imported_count=0,
            skipped_duplicates=len(messages),
            message_ids=[],
        )

    result = write_with_audit_outbox_idempotency(
        db,
        context,
        work=_do_batch_insert,
        audit_event=audit_event,
        outbox_event=outbox_event,
        resolve_existing=_resolve_existing,
    )

    # Update payload with actual counts
    return result


# ══════════════════════════════════════════════════════════════════════
# Read
# ══════════════════════════════════════════════════════════════════════

def get_message(db: Session, message_id: UUID) -> MessageRead | None:
    """Look up a single message by primary key."""
    row = db.execute(_SELECT_MESSAGE_BY_ID, {"message_id": message_id}).first()
    if row is None:
        return None
    return _msg_from_row(row)


def list_messages(
    db: Session,
    *,
    conversation_id: UUID,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[MessageRead], int]:
    """List messages for a conversation, ordered by message_time ASC.

    Supports infinite-scroll style pagination via page/page_size.
    """
    total = db.execute(_LIST_MESSAGES_COUNT, {"conversation_id": conversation_id}).scalar_one()
    offset = (page - 1) * page_size
    rows = db.execute(
        _LIST_MESSAGES,
        {
            "conversation_id": conversation_id,
            "page_size": page_size,
            "offset": offset,
        },
    ).all()
    items = [_msg_from_row(row) for row in rows]
    return items, total


# ══════════════════════════════════════════════════════════════════════
# Dedup helper
# ══════════════════════════════════════════════════════════════════════

def _find_duplicate(
    db: Session,
    *,
    event_source_id: UUID | None,
    content_hash: str,
    message_time: datetime,
) -> MessageRead | None:
    """Check if a message with the same dedup key already exists.

    Matches the ``UNIQUE(event_source_id, content_hash, message_time)`` constraint.
    """
    row = db.execute(
        _SELECT_MESSAGE_BY_DEDUP,
        {
            "event_source_id": event_source_id,
            "content_hash": content_hash,
            "message_time": message_time,
        },
    ).first()
    if row is None:
        return None
    return _msg_from_row(row)

