"""Data-access layer for ``neg_space_events`` table.

负空间记录 — tracks when the AI avoids topics, deletes sentences, or remains
silent. Provides insert, query, and aggregation operations.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID

from mneme.db.base import SessionLocal

logger = logging.getLogger(__name__)

# ── Column list ───────────────────────────────────────────────────────────────

_NEG_SPACE_EVENT_COLS = [
    "event_id", "agent_id", "conversation_id", "message_id",
    "event_category", "event_type", "trigger_text", "reason",
    "severity", "context_json", "created_at",
]

# ── SQL templates ─────────────────────────────────────────────────────────────

_INSERT_NEG_SPACE_EVENT = text("""
    INSERT INTO neg_space_events (
        event_id, agent_id, conversation_id, message_id,
        event_category, event_type, trigger_text, reason,
        severity, context_json
    ) VALUES (
        :event_id, :agent_id, :conversation_id, :message_id,
        :event_category, :event_type, :trigger_text, :reason,
        :severity, :context_json
    )
    RETURNING event_id
""").bindparams(
    bindparam("event_id", type_=PG_UUID(as_uuid=True)),
    bindparam("agent_id", type_=PG_UUID(as_uuid=True)),
    bindparam("conversation_id", type_=PG_UUID(as_uuid=True)),
    bindparam("message_id", type_=PG_UUID(as_uuid=True)),
    bindparam("context_json", type_=JSONB),
)

_SELECT_BY_ID = text("""
    SELECT event_id, agent_id, conversation_id, message_id,
           event_category, event_type, trigger_text, reason,
           severity, context_json, created_at
    FROM neg_space_events
    WHERE event_id = :event_id
""").bindparams(bindparam("event_id", type_=PG_UUID(as_uuid=True)))

_SELECT_WITH_FILTERS = """
    SELECT event_id, agent_id, conversation_id, message_id,
           event_category, event_type, trigger_text, reason,
           severity, context_json, created_at
    FROM neg_space_events
    WHERE 1=1
      AND (:event_category IS NULL OR event_category = :event_category)
      AND (:severity IS NULL OR severity = :severity)
      AND (:agent_id IS NULL OR agent_id = :agent_id)
      AND (:conversation_id IS NULL OR conversation_id = :conversation_id)
      AND (:created_after IS NULL OR created_at >= :created_after)
      AND (:created_before IS NULL OR created_at <= :created_before)
"""

_COUNT_FILTERED = """
    SELECT count(*) AS total
    FROM neg_space_events
    WHERE 1=1
      AND (:event_category IS NULL OR event_category = :event_category)
      AND (:severity IS NULL OR severity = :severity)
      AND (:agent_id IS NULL OR agent_id = :agent_id)
      AND (:conversation_id IS NULL OR conversation_id = :conversation_id)
      AND (:created_after IS NULL OR created_at >= :created_after)
      AND (:created_before IS NULL OR created_at <= :created_before)
"""

_SUMMARY_BY_AGENT = text("""
    SELECT
        COUNT(*) AS total_events,
        event_category,
        severity
    FROM neg_space_events
    WHERE (:agent_id IS NULL OR agent_id = :agent_id)
    GROUP BY event_category, severity
    ORDER BY event_category, severity
""").bindparams(bindparam("agent_id", type_=PG_UUID(as_uuid=True)))

_SUMMARY_BY_CONVERSATION = text("""
    SELECT
        COUNT(*) AS total_events,
        event_category,
        severity
    FROM neg_space_events
    WHERE conversation_id = :conversation_id
    GROUP BY event_category, severity
    ORDER BY event_category, severity
""").bindparams(bindparam("conversation_id", type_=PG_UUID(as_uuid=True)))

_LATEST_EVENT_BY_AGENT = text("""
    SELECT MAX(created_at) AS latest_event_at
    FROM neg_space_events
    WHERE agent_id = :agent_id
""").bindparams(bindparam("agent_id", type_=PG_UUID(as_uuid=True)))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _row_to_dict(row, column_names: list[str]) -> dict:
    import json as _json_mod

    d: dict = {}
    m = row._mapping if hasattr(row, "_mapping") else row
    for col in column_names:
        val = m.get(col)
        if isinstance(val, UUID):
            d[col] = str(val)
        elif isinstance(val, datetime):
            d[col] = val.isoformat()
        elif col == "context_json":
            if isinstance(val, str):
                try:
                    d[col] = _json_mod.loads(val)
                except (_json_mod.JSONDecodeError, TypeError):
                    d[col] = {}
            elif isinstance(val, dict):
                d[col] = val
            else:
                d[col] = {}
        else:
            d[col] = val
    return d


# ── Public API ─────────────────────────────────────────────────────────────────


def insert_neg_space_event(
    *,
    agent_id: UUID | None = None,
    conversation_id: UUID | None = None,
    message_id: UUID | None = None,
    event_category: str,
    event_type: str,
    trigger_text: str | None = None,
    reason: str | None = None,
    severity: str = "medium",
    context_json: dict[str, Any] | None = None,
) -> UUID:
    """Insert a new neg_space_events row and return its primary key."""
    event_id = uuid4()
    with SessionLocal() as db:
        try:
            new_id = db.execute(
                _INSERT_NEG_SPACE_EVENT,
                {
                    "event_id": event_id,
                    "agent_id": agent_id,
                    "conversation_id": conversation_id,
                    "message_id": message_id,
                    "event_category": event_category,
                    "event_type": event_type,
                    "trigger_text": trigger_text,
                    "reason": reason,
                    "severity": severity,
                    "context_json": context_json or {},
                },
            ).scalar_one()
            db.commit()
            logger.debug(
                "neg_space_event created: %s category=%s severity=%s",
                new_id, event_category, severity,
            )
            return new_id
        except Exception:
            db.rollback()
            raise


def get_neg_space_event_by_id(event_id: UUID) -> dict | None:
    """Fetch a neg_space_events row by primary key."""
    with SessionLocal() as db:
        row = db.execute(
            _SELECT_BY_ID, {"event_id": event_id}
        ).mappings().first()
        if row is None:
            return None
        return _row_to_dict(row, _NEG_SPACE_EVENT_COLS)


def get_neg_space_events(
    *,
    page: int = 1,
    page_size: int = 50,
    event_category: str | None = None,
    severity: str | None = None,
    agent_id: UUID | None = None,
    conversation_id: UUID | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
) -> tuple[list[dict], int]:
    """Return a page of neg_space_events with optional filters."""
    params: dict[str, Any] = {
        "event_category": event_category,
        "severity": severity,
        "agent_id": agent_id,
        "conversation_id": conversation_id,
        "created_after": created_after,
        "created_before": created_before,
        "limit": page_size,
        "offset": (page - 1) * page_size,
    }

    with SessionLocal() as db:
        total = db.execute(text(_COUNT_FILTERED), params).scalar_one()
        rows = db.execute(
            text(_SELECT_WITH_FILTERS + " ORDER BY created_at DESC LIMIT :limit OFFSET :offset"),
            params,
        ).mappings().all()
        items = [_row_to_dict(row, _NEG_SPACE_EVENT_COLS) for row in rows]
        return items, total


def get_neg_space_summary(
    *,
    agent_id: UUID | None = None,
    conversation_id: UUID | None = None,
) -> dict:
    """Return an aggregated summary of neg_space_events.

    If both agent_id and conversation_id are None, returns empty summary.
    """
    by_category: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    total_events = 0
    latest_event_at = None

    with SessionLocal() as db:
        # ── category + severity breakdown ──
        if conversation_id is not None:
            rows = db.execute(
                _SUMMARY_BY_CONVERSATION, {"conversation_id": conversation_id}
            ).mappings().all()
        else:
            rows = db.execute(
                _SUMMARY_BY_AGENT, {"agent_id": agent_id}
            ).mappings().all()

        for row in rows:
            cat = row["event_category"]
            sev = row["severity"]
            cnt = row["total_events"]
            by_category[cat] = by_category.get(cat, 0) + cnt
            by_severity[sev] = by_severity.get(sev, 0) + cnt
            total_events += cnt

        # ── latest event timestamp ──
        if agent_id is not None:
            latest_row = db.execute(
                _LATEST_EVENT_BY_AGENT, {"agent_id": agent_id}
            ).mappings().first()
            if latest_row and latest_row["latest_event_at"]:
                val = latest_row["latest_event_at"]
                latest_event_at = val.isoformat() if isinstance(val, datetime) else str(val)

    return {
        "total_events": total_events,
        "by_category": by_category,
        "by_severity": by_severity,
        "latest_event_at": latest_event_at,
    }
