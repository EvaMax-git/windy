"""Admin query helpers for audit_events and events tables.

Provides pure-SQL queries for the admin governance pages:
* audit_events listing / detail
* events (outbox) listing / detail
* event_deliveries for a given event
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from mneme.db.base import SessionLocal


# ── Audit Events ────────────────────────────────────────────────────────────────

_COUNT_AUDIT_EVENTS = text("""
    SELECT count(*) AS total
    FROM audit_events
    WHERE 1=1
      AND (:actor_type IS NULL OR actor_type = :actor_type)
      AND (:action IS NULL OR action LIKE :action)
      AND (:result IS NULL OR result = :result)
      AND (:object_type IS NULL OR object_type = :object_type)
      AND (:occurred_after IS NULL OR occurred_at >= :occurred_after)
      AND (:occurred_before IS NULL OR occurred_at <= :occurred_before)
""")

_QUERY_AUDIT_EVENTS = text("""
    SELECT
        audit_id,
        occurred_at,
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
    FROM audit_events
    WHERE 1=1
      AND (:actor_type IS NULL OR actor_type = :actor_type)
      AND (:action IS NULL OR action LIKE :action)
      AND (:result IS NULL OR result = :result)
      AND (:object_type IS NULL OR object_type = :object_type)
      AND (:occurred_after IS NULL OR occurred_at >= :occurred_after)
      AND (:occurred_before IS NULL OR occurred_at <= :occurred_before)
    ORDER BY occurred_at DESC
    LIMIT :limit OFFSET :offset
""")

_GET_AUDIT_EVENT_BY_ID = text("""
    SELECT
        audit_id,
        occurred_at,
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
    FROM audit_events
    WHERE audit_id = :audit_id
""")


def get_audit_events(
    *,
    page: int = 1,
    page_size: int = 50,
    actor_type: str | None = None,
    action: str | None = None,
    result: str | None = None,
    object_type: str | None = None,
    occurred_after: datetime | None = None,
    occurred_before: datetime | None = None,
) -> tuple[list[dict], int]:
    """Return a page of audit events with the given filters."""
    action_pattern = f"%{action}%" if action else None

    params = {
        "actor_type": actor_type,
        "action": action_pattern,
        "result": result,
        "object_type": object_type,
        "occurred_after": occurred_after,
        "occurred_before": occurred_before,
        "limit": page_size,
        "offset": (page - 1) * page_size,
    }

    with SessionLocal() as db:
        total = db.execute(_COUNT_AUDIT_EVENTS, params).scalar_one()
        rows = db.execute(_QUERY_AUDIT_EVENTS, params).mappings().all()
        items = [_audit_row_to_dict(row) for row in rows]
        return items, total


def get_audit_event_by_id(audit_id: UUID) -> dict | None:
    """Return a single audit event row by primary key, or None."""
    with SessionLocal() as db:
        row = (
            db.execute(
                _GET_AUDIT_EVENT_BY_ID,
                {"audit_id": audit_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        return _audit_row_to_dict(row)


def _audit_row_to_dict(row) -> dict:
    """Convert a SQLAlchemy RowMapping to a plain dict safe for JSON."""
    return {
        "audit_id": str(row["audit_id"]),
        "occurred_at": _isoformat(row["occurred_at"]),
        "actor": {
            "actor_type": row["actor_type"],
            "actor_id": str(row["actor_id"]) if row.get("actor_id") else None,
            "auth_context_type": row.get("auth_context_type"),
            "auth_context_id": str(row["auth_context_id"])
            if row.get("auth_context_id")
            else None,
        },
        "action": row["action"],
        "object_type": row.get("object_type"),
        "object_id": str(row["object_id"]) if row.get("object_id") else None,
        "project_id": str(row["project_id"]) if row.get("project_id") else None,
        "result": row["result"],
        "reason_code": row.get("reason_code"),
        "sensitivity_level": row["sensitivity_level"],
        "correlation_id": str(row["correlation_id"]),
        "request_id": str(row["request_id"]),
        "review_item_id": str(row["review_item_id"])
        if row.get("review_item_id")
        else None,
        "diff_summary": _parse_json_field(row.get("diff_summary")),
        "metadata_json": _parse_json_field(row.get("metadata_json")),
    }


# ── Events (Outbox) ─────────────────────────────────────────────────────────────

_COUNT_EVENTS = text("""
    SELECT count(*) AS total
    FROM events
    WHERE 1=1
      AND (:event_type IS NULL OR event_type LIKE :event_type)
      AND (:publish_state IS NULL OR publish_state = :publish_state)
      AND (:aggregate_type IS NULL OR aggregate_type = :aggregate_type)
      AND (:occurred_after IS NULL OR occurred_at >= :occurred_after)
      AND (:occurred_before IS NULL OR occurred_at <= :occurred_before)
""")

_QUERY_EVENTS = text("""
    SELECT
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
        occurred_at,
        committed_at,
        published_at,
        last_error
    FROM events
    WHERE 1=1
      AND (:event_type IS NULL OR event_type LIKE :event_type)
      AND (:publish_state IS NULL OR publish_state = :publish_state)
      AND (:aggregate_type IS NULL OR aggregate_type = :aggregate_type)
      AND (:occurred_after IS NULL OR occurred_at >= :occurred_after)
      AND (:occurred_before IS NULL OR occurred_at <= :occurred_before)
    ORDER BY occurred_at DESC
    LIMIT :limit OFFSET :offset
""")

_GET_EVENT_BY_ID = text("""
    SELECT
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
        occurred_at,
        committed_at,
        published_at,
        last_error
    FROM events
    WHERE event_id = :event_id
""")

_QUERY_EVENT_DELIVERIES = text("""
    SELECT
        delivery_id,
        event_id,
        consumer_name,
        delivery_state,
        dispatch_attempts,
        last_dispatched_at,
        acknowledged_at,
        failed_at,
        last_error,
        lease_expires_at,
        created_at,
        updated_at
    FROM event_deliveries
    WHERE event_id = :event_id
    ORDER BY dispatch_attempts DESC
""")


def get_events(
    *,
    page: int = 1,
    page_size: int = 50,
    event_type: str | None = None,
    publish_state: str | None = None,
    aggregate_type: str | None = None,
    occurred_after: datetime | None = None,
    occurred_before: datetime | None = None,
) -> tuple[list[dict], int]:
    """Return a page of events with the given filters."""
    event_type_pattern = f"%{event_type}%" if event_type else None

    params = {
        "event_type": event_type_pattern,
        "publish_state": publish_state,
        "aggregate_type": aggregate_type,
        "occurred_after": occurred_after,
        "occurred_before": occurred_before,
        "limit": page_size,
        "offset": (page - 1) * page_size,
    }

    with SessionLocal() as db:
        total = db.execute(_COUNT_EVENTS, params).scalar_one()
        rows = db.execute(_QUERY_EVENTS, params).mappings().all()
        items = [_event_row_to_dict(row) for row in rows]
        return items, total


def get_event_by_id(event_id: UUID) -> dict | None:
    """Return a single event row by primary key, or None."""
    with SessionLocal() as db:
        row = (
            db.execute(
                _GET_EVENT_BY_ID,
                {"event_id": event_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        return _event_row_to_dict(row)


def get_event_deliveries_for_event(event_id: UUID) -> list[dict]:
    """Return all deliveries for a given event."""
    with SessionLocal() as db:
        rows = (
            db.execute(
                _QUERY_EVENT_DELIVERIES,
                {"event_id": event_id},
            )
            .mappings()
            .all()
        )
        return [_delivery_row_to_dict(row) for row in rows]


def _event_row_to_dict(row) -> dict:
    """Convert a SQLAlchemy RowMapping to a plain dict safe for JSON."""
    return {
        "event_id": str(row["event_id"]),
        "event_type": row["event_type"],
        "aggregate_type": row["aggregate_type"],
        "aggregate_id": str(row["aggregate_id"]),
        "aggregate_version": row["aggregate_version"],
        "correlation_id": str(row["correlation_id"]),
        "causation_id": str(row["causation_id"])
        if row.get("causation_id")
        else None,
        "idempotency_key": row["idempotency_key"],
        "producer": row["producer"],
        "payload_json": row.get("payload_json") or {},
        "visibility": row["visibility"],
        "publish_state": row["publish_state"],
        "occurred_at": _isoformat(row["occurred_at"]),
        "committed_at": _isoformat(row["committed_at"]),
        "published_at": _isoformat(row.get("published_at")),
        "last_error": row.get("last_error"),
    }


def _delivery_row_to_dict(row) -> dict:
    """Convert a delivery RowMapping to a plain dict."""
    return {
        "delivery_id": str(row["delivery_id"]),
        "event_id": str(row["event_id"]),
        "consumer_name": row["consumer_name"],
        "delivery_state": row["delivery_state"],
        "dispatch_attempts": row["dispatch_attempts"],
        "last_dispatched_at": _isoformat(row.get("last_dispatched_at")),
        "acknowledged_at": _isoformat(row.get("acknowledged_at")),
        "failed_at": _isoformat(row.get("failed_at")),
        "last_error": row.get("last_error"),
        "lease_expires_at": _isoformat(row.get("lease_expires_at")),
        "created_at": _isoformat(row.get("created_at")),
        "updated_at": _isoformat(row.get("updated_at")),
    }


# ── Helpers ─────────────────────────────────────────────────────────────────────


def _isoformat(dt: datetime | str | None) -> str | None:
    """Return ISO-8601 string for *dt*, or None.

    SQLite returns strings instead of datetime objects; convert them
    on-the-fly so this helper works on both PostgreSQL and SQLite.
    """
    if dt is None:
        return None
    if isinstance(dt, str):
        # SQLite compatibility: parse the string into a datetime first
        try:
            dt = datetime.fromisoformat(dt)
        except (ValueError, TypeError):
            # If it's not ISO-8601, try parsing as 'YYYY-MM-DD HH:MM:SS'
            try:
                dt = datetime.strptime(dt, "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                return dt  # last resort: return the raw string
    return dt.isoformat()


def _as_uuid(value) -> UUID:
    """Coerce a value to UUID."""
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def _parse_json_field(value: str | dict | None) -> dict:
    """Parse a JSON field that may be a string (SQLite) or dict (PostgreSQL)."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            import json
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}
