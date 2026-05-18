"""Idempotency key processing for write requests.

The ``events`` table has a ``UNIQUE(idempotency_key)`` constraint that serves as
the database-level guard against duplicate writes.  This module provides helpers
that:

1. Check whether an idempotency key has already been used (pre-write check).
2. Resolve a previously created business-object id from an existing event
   so the caller can return the same response body (idempotent reply).

Concurrency note
----------------

The pre-write check runs *outside* the write transaction.  In the rare event
that two concurrent requests arrive with the same idempotency key, one will
pass the check and commit while the other will encounter a ``UNIQUE``
violation on ``events.idempotency_key``.  The caller is expected to catch
:class:`IdempotencyConflict` and retry the check → resolve path.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session


class IdempotencyConflict(Exception):
    """Raised when a write with a previously used idempotency key is detected
    *after* the pre-check (i.e. a concurrent-duplicate race was lost).

    Callers should catch this, look up the existing event / business object,
    and return the idempotent response body instead of propagating the error.
    """

    def __init__(self, idempotency_key: str, aggregate_type: str) -> None:
        super().__init__(
            f"idempotency key '{idempotency_key}' already used for '{aggregate_type}'"
        )
        self.idempotency_key = idempotency_key
        self.aggregate_type = aggregate_type


_CHECK_KEY = text(
    """
    SELECT aggregate_id
    FROM events
    WHERE idempotency_key = :idempotency_key
      AND aggregate_type = :aggregate_type
    LIMIT 1
    """
)


def check_idempotency_key(
    db: Session,
    *,
    idempotency_key: str | None,
    aggregate_type: str,
) -> UUID | None:
    """Check whether *idempotency_key* has already been used for *aggregate_type*.

    Returns the business object id (``aggregate_id``) if the key exists,
    ``None`` otherwise.

    When ``idempotency_key`` is ``None`` this function short-circuits and
    returns ``None`` (the write is not idempotency-guarded).
    """
    if idempotency_key is None:
        return None

    row = db.execute(
        _CHECK_KEY,
        {"idempotency_key": idempotency_key, "aggregate_type": aggregate_type},
    ).first()
    if row is None:
        return None
    return _as_uuid(row.aggregate_id)


_CHECK_KEY_ANY = text(
    """
    SELECT event_id, aggregate_type, aggregate_id
    FROM events
    WHERE idempotency_key = :idempotency_key
    LIMIT 1
    """
)


def check_idempotency_key_any(
    db: Session,
    *,
    idempotency_key: str,
) -> tuple[UUID, str, UUID] | None:
    """Check whether *idempotency_key* has been used for *any* aggregate type.

    Returns ``(event_id, aggregate_type, aggregate_id)`` if the key exists,
    ``None`` otherwise.

    Useful when a request body must be validated against the previously stored
    aggregate type (mismatched-type requests with the same key should return a
    conflict error, not a different object).
    """
    row = db.execute(
        _CHECK_KEY_ANY,
        {"idempotency_key": idempotency_key},
    ).first()
    if row is None:
        return None
    return (
        _as_uuid(row.event_id),
        str(row.aggregate_type),
        _as_uuid(row.aggregate_id),
    )


_IDEMPOTENT_REPLAY = text(
    """
    SELECT payload_json
    FROM events
    WHERE event_id = :event_id
    """
)


def load_idempotent_payload(db: Session, event_id: UUID) -> dict[str, Any]:
    """Load the ``payload_json`` of the event tied to an idempotency key.

    Returns an empty dict if the event no longer exists.
    """
    row = db.execute(_IDEMPOTENT_REPLAY, {"event_id": event_id}).first()
    if row is None:
        return {}
    payload = row.payload_json
    if isinstance(payload, dict):
        return payload
    return {}


def _as_uuid(value: Any) -> UUID:
    if isinstance(value, UUID):
        return value
    return UUID(str(value))
