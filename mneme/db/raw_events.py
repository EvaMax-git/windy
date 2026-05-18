"""Raw Events CRUD with audit + outbox + idempotency (P4-03).

Key features:
* SHA-256 ``payload_hash`` is auto-computed from ``payload_json``.
* ``text_preview`` is auto-extracted (first 500 chars of JSON).
* ``idempotency_key`` auto-derived: explicit > source_platform+external_event_id > hash+time.
* ``retention_until`` defaults to ``event_time + 365 days``.
* ``pii_flags`` defaults to ``[]`` (Phase 4).
* ``UNIQUE(idempotency_key)`` + ``UNIQUE(event_source_id, payload_hash, event_time)``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
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
from mneme.schemas.conversations import RawEventCreate, RawEventRead


# ══════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════

def compute_payload_hash(payload_json: dict[str, Any]) -> str:
    """SHA-256 of canonical JSON payload."""
    raw = json.dumps(payload_json, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def extract_text_preview(payload_json: dict[str, Any]) -> str | None:
    """Extract first 500 chars of textual content from payload_json."""
    for key in ("text", "content", "content_text", "message"):
        if key in payload_json and isinstance(payload_json[key], str):
            return payload_json[key][:500]
    raw = json.dumps(payload_json, ensure_ascii=False)
    return raw[:500] if raw else None


def derive_idempotency_key(payload: RawEventCreate, payload_hash: str) -> str:
    """Derive idempotency_key: explicit > platform:external_id > hash:time."""
    if payload.idempotency_key:
        return payload.idempotency_key
    if payload.source_platform and payload.external_event_id:
        return f"{payload.source_platform}:{payload.external_event_id}"
    return f"{payload_hash[:16]}:{payload.event_time.isoformat()}"


def _retention_until(event_time: datetime) -> datetime:
    return event_time + timedelta(days=365)


# ══════════════════════════════════════════════════════════════════════
# SQL
# ══════════════════════════════════════════════════════════════════════

_INSERT = text("""
    INSERT INTO raw_events (
      raw_event_id, project_id, event_source_id, conversation_id, message_id,
      raw_event_type, source_platform, external_event_id, event_time,
      payload_hash, payload_json, text_preview, sensitivity_level,
      pii_flags, retention_until, import_run_id, idempotency_key
    ) VALUES (
      :raw_event_id, :project_id, :event_source_id, :conversation_id, :message_id,
      :raw_event_type, :source_platform, :external_event_id, :event_time,
      :payload_hash, :payload_json, :text_preview, :sensitivity_level,
      :pii_flags, :retention_until, :import_run_id, :idempotency_key
    )
    RETURNING
      raw_event_id, project_id, event_source_id, conversation_id, message_id,
      raw_event_type, source_platform, external_event_id, event_time,
      payload_hash, payload_json, text_preview, sensitivity_level,
      pii_flags, retention_until, import_run_id, idempotency_key, created_at
""").bindparams(
    bindparam("raw_event_id", type_=PG_UUID(as_uuid=True)),
    bindparam("project_id", type_=PG_UUID(as_uuid=True)),
    bindparam("event_source_id", type_=PG_UUID(as_uuid=True)),
    bindparam("conversation_id", type_=PG_UUID(as_uuid=True)),
    bindparam("message_id", type_=PG_UUID(as_uuid=True)),
    bindparam("import_run_id", type_=PG_UUID(as_uuid=True)),
)

_SELECT_BY_ID = text("""
    SELECT
      raw_event_id, project_id, event_source_id, conversation_id, message_id,
      raw_event_type, source_platform, external_event_id, event_time,
      payload_hash, payload_json, text_preview, sensitivity_level,
      pii_flags, retention_until, import_run_id, idempotency_key, created_at
    FROM raw_events WHERE raw_event_id = :raw_event_id
""").bindparams(bindparam("raw_event_id", type_=PG_UUID(as_uuid=True)))

_SELECT_BY_KEY = text("""
    SELECT
      raw_event_id, project_id, event_source_id, conversation_id, message_id,
      raw_event_type, source_platform, external_event_id, event_time,
      payload_hash, payload_json, text_preview, sensitivity_level,
      pii_flags, retention_until, import_run_id, idempotency_key, created_at
    FROM raw_events WHERE idempotency_key = :idempotency_key LIMIT 1
""")

_LIST_COUNT = text("""
    SELECT count(*) FROM raw_events
    WHERE (:conversation_id IS NULL OR conversation_id = :conversation_id)
      AND (:event_source_id IS NULL OR event_source_id = :event_source_id)
""").bindparams(
    bindparam("conversation_id", type_=PG_UUID(as_uuid=True)),
    bindparam("event_source_id", type_=PG_UUID(as_uuid=True)),
)

_LIST = text("""
    SELECT
      raw_event_id, project_id, event_source_id, conversation_id, message_id,
      raw_event_type, source_platform, external_event_id, event_time,
      payload_hash, payload_json, text_preview, sensitivity_level,
      pii_flags, retention_until, import_run_id, idempotency_key, created_at
    FROM raw_events
    WHERE (:conversation_id IS NULL OR conversation_id = :conversation_id)
      AND (:event_source_id IS NULL OR event_source_id = :event_source_id)
    ORDER BY event_time DESC, created_at DESC
    LIMIT :page_size OFFSET :offset
""").bindparams(
    bindparam("conversation_id", type_=PG_UUID(as_uuid=True)),
    bindparam("event_source_id", type_=PG_UUID(as_uuid=True)),
)


def _row_to_read(row: Any) -> RawEventRead:
    data = dict(row._mapping)
    for field in ("pii_flags", "payload_json"):
        if isinstance(data.get(field), str):
            data[field] = json.loads(data[field])
        elif data.get(field) is None:
            data[field] = [] if field == "pii_flags" else {}
    return RawEventRead.model_validate(data)


# ══════════════════════════════════════════════════════════════════════
# Create
# ══════════════════════════════════════════════════════════════════════

def create_raw_event(
    db: Session,
    context: RequestContext,
    *,
    payload: RawEventCreate,
) -> RawEventRead:
    raw_event_id = uuid4()
    payload_hash = compute_payload_hash(payload.payload_json)
    text_preview = extract_text_preview(payload.payload_json)
    idempotency_key = derive_idempotency_key(payload, payload_hash)
    object_type = "raw_event"

    existing = _find_by_key(db, idempotency_key)
    if existing is not None:
        return existing

    outbox_event = OutboxEvent(
        event_type="raw_event.created",
        aggregate_type=object_type,
        aggregate_id=raw_event_id,
        aggregate_version=1,
        idempotency_key=idempotency_key,
        producer="mneme-api",
        payload_json={
            "raw_event_id": str(raw_event_id),
            "raw_event_type": payload.raw_event_type.value,
            "payload_hash": payload_hash,
        },
        visibility="internal",
        publish_state="pending",
    )

    audit_event = AuditEvent(
        action="raw_event.create",
        result="success",
        object_type=object_type,
        object_id=raw_event_id,
        project_id=payload.project_id,
        sensitivity_level=payload.sensitivity_level.value,
    )

    def _do_insert(db2: Session) -> RawEventRead:
        row = db2.execute(_INSERT, {
            "raw_event_id": raw_event_id,
            "project_id": payload.project_id,
            "event_source_id": payload.event_source_id,
            "conversation_id": payload.conversation_id,
            "message_id": payload.message_id,
            "raw_event_type": payload.raw_event_type.value,
            "source_platform": payload.source_platform,
            "external_event_id": payload.external_event_id,
            "event_time": payload.event_time,
            "payload_hash": payload_hash,
            "payload_json": json.dumps(payload.payload_json, ensure_ascii=False),
            "text_preview": text_preview,
            "sensitivity_level": payload.sensitivity_level.value,
            "pii_flags": json.dumps([]),
            "retention_until": _retention_until(payload.event_time),
            "import_run_id": payload.import_run_id,
            "idempotency_key": idempotency_key,
        }).one()
        return _row_to_read(row)

    def _resolve_existing(db2: Session, aggregate_id: UUID) -> RawEventRead:
        row = db2.execute(_SELECT_BY_ID, {"raw_event_id": aggregate_id}).first()
        if row is None:
            raise LookupError(f"raw_event {aggregate_id} not found during idempotent replay")
        return _row_to_read(row)

    return write_with_audit_outbox_idempotency(
        db, context,
        work=_do_insert,
        audit_event=audit_event,
        outbox_event=outbox_event,
        resolve_existing=_resolve_existing,
    )


# ══════════════════════════════════════════════════════════════════════
# Read
# ══════════════════════════════════════════════════════════════════════

def get_raw_event(db: Session, raw_event_id: UUID) -> RawEventRead | None:
    row = db.execute(_SELECT_BY_ID, {"raw_event_id": raw_event_id}).first()
    return _row_to_read(row) if row is not None else None


def list_raw_events(
    db: Session,
    *,
    conversation_id: UUID | None = None,
    event_source_id: UUID | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[RawEventRead], int]:
    total = db.execute(_LIST_COUNT, {
        "conversation_id": conversation_id,
        "event_source_id": event_source_id,
    }).scalar_one()
    offset = (page - 1) * page_size
    rows = db.execute(_LIST, {
        "conversation_id": conversation_id,
        "event_source_id": event_source_id,
        "page_size": page_size,
        "offset": offset,
    }).all()
    return [_row_to_read(r) for r in rows], total


# ══════════════════════════════════════════════════════════════════════
# Dedup
# ══════════════════════════════════════════════════════════════════════

def _find_by_key(db: Session, idempotency_key: str) -> RawEventRead | None:
    row = db.execute(_SELECT_BY_KEY, {"idempotency_key": idempotency_key}).first()
    return _row_to_read(row) if row is not None else None
