"""Event Source CRUD with audit + outbox + idempotency.

P4-02 — event_source table.

Every write mutation is wrapped in
:func:`mneme.db.audit.write_with_audit_outbox_idempotency`.
"""

from __future__ import annotations

import json
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
from mneme.schemas.conversations import (
    EventSourceCreate,
    EventSourceRead,
)


_INSERT_EVENT_SOURCE = text(
    """
    INSERT INTO event_source (
      event_source_id,
      conversation_id,
      source_platform,
      external_conversation_id,
      source_account_id,
      source_uri,
      participants_json,
      time_range_start,
      time_range_end,
      import_run_id,
      metadata_json
    )
    VALUES (
      :event_source_id,
      :conversation_id,
      :source_platform,
      :external_conversation_id,
      :source_account_id,
      :source_uri,
      :participants_json,
      :time_range_start,
      :time_range_end,
      :import_run_id,
      :metadata_json
    )
    RETURNING
      event_source_id,
      conversation_id,
      source_platform,
      external_conversation_id,
      source_account_id,
      source_uri,
      participants_json,
      time_range_start,
      time_range_end,
      import_run_id,
      metadata_json,
      created_at,
      updated_at
    """
).bindparams(
    bindparam("event_source_id", type_=PG_UUID(as_uuid=True)),
    bindparam("conversation_id", type_=PG_UUID(as_uuid=True)),
    bindparam("import_run_id", type_=PG_UUID(as_uuid=True)),
)


_SELECT_EVENT_SOURCE_BY_ID = text(
    """
    SELECT
      event_source_id,
      conversation_id,
      source_platform,
      external_conversation_id,
      source_account_id,
      source_uri,
      participants_json,
      time_range_start,
      time_range_end,
      import_run_id,
      metadata_json,
      created_at,
      updated_at
    FROM event_source
    WHERE event_source_id = :event_source_id
    """
).bindparams(bindparam("event_source_id", type_=PG_UUID(as_uuid=True)))


_LIST_EVENT_SOURCES_BY_CONVERSATION = text(
    """
    SELECT
      event_source_id,
      conversation_id,
      source_platform,
      external_conversation_id,
      source_account_id,
      source_uri,
      participants_json,
      time_range_start,
      time_range_end,
      import_run_id,
      metadata_json,
      created_at,
      updated_at
    FROM event_source
    WHERE conversation_id = :conversation_id
    ORDER BY created_at ASC
    """
).bindparams(bindparam("conversation_id", type_=PG_UUID(as_uuid=True)))


def _es_from_row(row: Any) -> EventSourceRead:
    data = dict(row._mapping)
    # participants_json and metadata_json come as dicts from asyncpg, or strings
    if isinstance(data.get("participants_json"), str):
        data["participants_json"] = json.loads(data["participants_json"])
    elif data.get("participants_json") is None:
        data["participants_json"] = []
    if isinstance(data.get("metadata_json"), str):
        data["metadata_json"] = json.loads(data["metadata_json"])
    elif data.get("metadata_json") is None:
        data["metadata_json"] = {}
    return EventSourceRead.model_validate(data)


# ══════════════════════════════════════════════════════════════════════
# Create
# ══════════════════════════════════════════════════════════════════════

def create_event_source(
    db: Session,
    context: RequestContext,
    *,
    conversation_id: UUID,
    payload: EventSourceCreate,
    project_id: UUID | None = None,
) -> EventSourceRead:
    """Create an event source segment under a conversation."""
    event_source_id = uuid4()
    object_type = "event_source"

    outbox_event = OutboxEvent(
        event_type="event_source.created",
        aggregate_type=object_type,
        aggregate_id=event_source_id,
        aggregate_version=1,
        idempotency_key=context.idempotency_key or "",
        producer="mneme-api",
        payload_json={
            "event_source_id": str(event_source_id),
            "conversation_id": str(conversation_id),
            "source_platform": payload.source_platform,
        },
        visibility="internal",
        publish_state="pending",
    )

    audit_event = AuditEvent(
        action="event_source.create",
        result="success",
        object_type=object_type,
        object_id=event_source_id,
        project_id=project_id,
        sensitivity_level="private",
    )

    def _do_insert(db2: Session) -> EventSourceRead:
        row = db2.execute(
            _INSERT_EVENT_SOURCE,
            {
                "event_source_id": event_source_id,
                "conversation_id": conversation_id,
                "source_platform": payload.source_platform,
                "external_conversation_id": payload.external_conversation_id,
                "source_account_id": payload.source_account_id,
                "source_uri": payload.source_uri,
                "participants_json": json.dumps(payload.participants_json),
                "time_range_start": payload.time_range_start,
                "time_range_end": payload.time_range_end,
                "import_run_id": payload.import_run_id,
                "metadata_json": json.dumps(payload.metadata_json),
            },
        ).one()
        return _es_from_row(row)

    def _resolve_existing(db2: Session, aggregate_id: UUID) -> EventSourceRead:
        row = db2.execute(_SELECT_EVENT_SOURCE_BY_ID, {"event_source_id": aggregate_id}).first()
        if row is None:
            raise LookupError(f"event_source {aggregate_id} not found during idempotent replay")
        return _es_from_row(row)

    return write_with_audit_outbox_idempotency(
        db,
        context,
        work=_do_insert,
        audit_event=audit_event,
        outbox_event=outbox_event,
        resolve_existing=_resolve_existing,
    )


# ══════════════════════════════════════════════════════════════════════
# Read
# ══════════════════════════════════════════════════════════════════════

def get_event_source(db: Session, event_source_id: UUID) -> EventSourceRead | None:
    """Look up an event source by primary key."""
    row = db.execute(_SELECT_EVENT_SOURCE_BY_ID, {"event_source_id": event_source_id}).first()
    if row is None:
        return None
    return _es_from_row(row)


def list_event_sources(db: Session, *, conversation_id: UUID) -> list[EventSourceRead]:
    """List all event sources for a conversation."""
    rows = db.execute(
        _LIST_EVENT_SOURCES_BY_CONVERSATION,
        {"conversation_id": conversation_id},
    ).all()
    return [_es_from_row(row) for row in rows]

