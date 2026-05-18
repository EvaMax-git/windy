"""Event Sourcing — append-only domain event store (L7-01).

``event_log`` is the **append-only domain event store** for the entire system.
It is *separate* from ``events`` (the outbox table used for pub/sub dispatch).

Key principles
--------------
* Append-only — rows are never mutated or deleted after insertion.
* Every state-changing operation appends one or more ``event_log`` rows.
* ``(stream_type, stream_id, stream_version)`` is UNIQUE, ensuring an
  unbroken version chain per stream.
* Consumers replay from ``event_log`` to rebuild read models or sync state
  to federation peers.

Stream types
------------
``memory``, ``conversation``, ``message``, ``knowledge_document``,
``knowledge_chunk``, ``asset``, ``agent``, ``project``
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session

from mneme.schemas.events import (
    EventLogEntryRead,
    EventLogFilterParams,
)

import logging
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# SQL statements
# ═══════════════════════════════════════════════════════════════════════

_APPEND_EVENT_LOG = text(r"""
    INSERT INTO event_log (
        log_id, stream_type, stream_id, stream_version,
        event_type, correlation_id, causation_id,
        actor_type, actor_id,
        payload_json, metadata_json,
        committed_at, project_id
    )
    VALUES (
        :log_id, :stream_type, :stream_id, :stream_version,
        :event_type, :correlation_id, :causation_id,
        :actor_type, :actor_id,
        :payload_json, :metadata_json,
        :committed_at, :project_id
    )
    RETURNING
        log_id, stream_type, stream_id, stream_version,
        event_type, correlation_id, causation_id,
        actor_type, actor_id,
        payload_json, metadata_json,
        committed_at, project_id
""").bindparams(
    bindparam("log_id", type_=PG_UUID(as_uuid=True)),
    bindparam("stream_id", type_=PG_UUID(as_uuid=True)),
    bindparam("correlation_id", type_=PG_UUID(as_uuid=True)),
    bindparam("causation_id", type_=PG_UUID(as_uuid=True)),
    bindparam("actor_id", type_=PG_UUID(as_uuid=True)),
    bindparam("project_id", type_=PG_UUID(as_uuid=True)),
)

_SELECT_STREAM = text(r"""
    SELECT
        log_id, stream_type, stream_id, stream_version,
        event_type, correlation_id, causation_id,
        actor_type, actor_id,
        payload_json, metadata_json,
        committed_at, project_id
    FROM event_log
    WHERE stream_type = :stream_type
      AND stream_id = :stream_id
    ORDER BY stream_version ASC
    LIMIT :limit OFFSET :offset
""")

_SELECT_STREAM_SINCE = text(r"""
    SELECT
        log_id, stream_type, stream_id, stream_version,
        event_type, correlation_id, causation_id,
        actor_type, actor_id,
        payload_json, metadata_json,
        committed_at, project_id
    FROM event_log
    WHERE stream_type = :stream_type
      AND stream_id = :stream_id
      AND stream_version > :after_version
    ORDER BY stream_version ASC
    LIMIT :limit OFFSET :offset
""")

_COUNT_STREAM = text(r"""
    SELECT COUNT(*) FROM event_log
    WHERE stream_type = :stream_type
      AND stream_id = :stream_id
""")

_LATEST_VERSION = text(r"""
    SELECT COALESCE(MAX(stream_version), 0) FROM event_log
    WHERE stream_type = :stream_type
      AND stream_id = :stream_id
""")

_LIST_EVENTS = text(r"""
    SELECT
        log_id, stream_type, stream_id, stream_version,
        event_type, correlation_id, causation_id,
        actor_type, actor_id,
        payload_json, metadata_json,
        committed_at, project_id
    FROM event_log
    WHERE (:project_id IS NULL OR project_id = :project_id)
      AND (:stream_type IS NULL OR stream_type = :stream_type)
      AND (:event_type IS NULL OR event_type = :event_type)
      AND (:since IS NULL OR committed_at >= :since)
      AND (:until IS NULL OR committed_at <= :until)
    ORDER BY committed_at DESC
    LIMIT :limit OFFSET :offset
""")

_COUNT_EVENTS = text(r"""
    SELECT COUNT(*) FROM event_log
    WHERE (:project_id IS NULL OR project_id = :project_id)
      AND (:stream_type IS NULL OR stream_type = :stream_type)
      AND (:event_type IS NULL OR event_type = :event_type)
      AND (:since IS NULL OR committed_at >= :since)
      AND (:until IS NULL OR committed_at <= :until)
""")


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════

def _row_to_entry(row: Any) -> EventLogEntryRead:
    data = dict(row._mapping)
    for col in ("payload_json", "metadata_json"):
        if isinstance(data.get(col), str):
            data[col] = json.loads(data[col])
        elif data.get(col) is None:
            data[col] = {}
    return EventLogEntryRead.model_validate(data)


# ═══════════════════════════════════════════════════════════════════════
# Append (the only write operation)
# ═══════════════════════════════════════════════════════════════════════

def append_event_log(
    db: Session,
    *,
    stream_type: str,
    stream_id: UUID,
    event_type: str,
    payload: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    correlation_id: UUID | None = None,
    causation_id: UUID | None = None,
    actor_type: str | None = None,
    actor_id: UUID | None = None,
    project_id: UUID | None = None,
    committed_at: datetime | None = None,
) -> EventLogEntryRead:
    """Append one event to the event log.

    Automatically determines the next ``stream_version`` by querying the
    current max version for ``(stream_type, stream_id)``.

    Returns the newly inserted row.
    """
    latest = db.execute(
        _LATEST_VERSION,
        {"stream_type": stream_type, "stream_id": stream_id},
    ).scalar_one()

    log_id = uuid4()
    row = db.execute(
        _APPEND_EVENT_LOG,
        {
            "log_id": log_id,
            "stream_type": stream_type,
            "stream_id": stream_id,
            "stream_version": latest + 1,
            "event_type": event_type,
            "correlation_id": correlation_id,
            "causation_id": causation_id,
            "actor_type": actor_type,
            "actor_id": actor_id,
            "payload_json": json.dumps(payload or {}),
            "metadata_json": json.dumps(metadata or {}),
            "committed_at": committed_at or datetime.now(timezone.utc),
            "project_id": project_id,
        },
    ).one()

    return _row_to_entry(row)


def append_event_log_batch(
    db: Session,
    *,
    stream_type: str,
    stream_id: UUID,
    events: list[dict[str, Any]],
    correlation_id: UUID | None = None,
    actor_type: str | None = None,
    actor_id: UUID | None = None,
    project_id: UUID | None = None,
) -> list[EventLogEntryRead]:
    """Atomically append multiple events to a single stream.

    Each dict in *events* must contain at minimum ``event_type``.
    Optional keys: ``payload``, ``metadata``, ``causation_id``.
    """
    latest = db.execute(
        _LATEST_VERSION,
        {"stream_type": stream_type, "stream_id": stream_id},
    ).scalar_one()

    results: list[EventLogEntryRead] = []
    now = datetime.now(timezone.utc)

    for i, ev in enumerate(events):
        log_id = uuid4()
        row = db.execute(
            _APPEND_EVENT_LOG,
            {
                "log_id": log_id,
                "stream_type": stream_type,
                "stream_id": stream_id,
                "stream_version": latest + 1 + i,
                "event_type": ev["event_type"],
                "correlation_id": correlation_id,
                "causation_id": ev.get("causation_id"),
                "actor_type": actor_type,
                "actor_id": actor_id,
                "payload_json": json.dumps(ev.get("payload", {})),
                "metadata_json": json.dumps(ev.get("metadata", {})),
                "committed_at": now,
                "project_id": project_id,
            },
        ).one()
        results.append(_row_to_entry(row))

    return results


# ═══════════════════════════════════════════════════════════════════════
# Read (replay)
# ═══════════════════════════════════════════════════════════════════════

def read_stream(
    db: Session,
    *,
    stream_type: str,
    stream_id: UUID,
    after_version: int = 0,
    page: int = 1,
    page_size: int = 100,
) -> tuple[list[EventLogEntryRead], int]:
    """Replay events for a stream, ordered by version ascending."""
    offset = (max(page, 1) - 1) * page_size

    if after_version > 0:
        rows = db.execute(
            _SELECT_STREAM_SINCE,
            {
                "stream_type": stream_type,
                "stream_id": stream_id,
                "after_version": after_version,
                "limit": page_size,
                "offset": offset,
            },
        ).all()
    else:
        rows = db.execute(
            _SELECT_STREAM,
            {
                "stream_type": stream_type,
                "stream_id": stream_id,
                "limit": page_size,
                "offset": offset,
            },
        ).all()

    total = db.execute(
        _COUNT_STREAM,
        {"stream_type": stream_type, "stream_id": stream_id},
    ).scalar_one()

    return [_row_to_entry(r) for r in rows], total or 0


def list_event_log(
    db: Session,
    *,
    filters: EventLogFilterParams | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[EventLogEntryRead], int]:
    """List event log entries with optional filtering, newest first."""
    offset = (max(page, 1) - 1) * page_size

    params: dict[str, Any] = {
        "project_id": filters.project_id if filters else None,
        "stream_type": filters.stream_type if filters else None,
        "event_type": filters.event_type if filters else None,
        "since": filters.since if filters else None,
        "until": filters.until if filters else None,
        "limit": page_size,
        "offset": offset,
    }

    total = db.execute(_COUNT_EVENTS, params).scalar_one()
    rows = db.execute(_LIST_EVENTS, params).all()

    return [_row_to_entry(r) for r in rows], total or 0


def get_latest_stream_version(
    db: Session,
    *,
    stream_type: str,
    stream_id: UUID,
) -> int:
    """Return the latest version for a stream (0 if no events exist)."""
    return db.execute(
        _LATEST_VERSION,
        {"stream_type": stream_type, "stream_id": stream_id},
    ).scalar_one()
