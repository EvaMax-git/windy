"""P2-05 Review Items data-access layer.

Provides pure-SQL queries against the ``review_items`` table used by the
review API routes.  All queries use SQLAlchemy ``text()`` so they align
exactly with the DDL column names in ``0001_baseline_45_tables.py``.

State machine
-------------
The ``status`` column follows this state machine:

* ``pending`` → ``in_review`` → ``approved`` / ``rejected``
* ``pending`` → ``cancelled``
* ``pending`` → ``expired``

Irreversible transitions are enforced by application-level guards (the
``WHERE`` clause checks the current status before updating).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from mneme.db.base import SessionLocal

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# SQL templates
# ──────────────────────────────────────────────────────────────────────────────

_COUNT_REVIEW_ITEMS = text("""
    SELECT count(*) AS total
    FROM review_items
    WHERE 1=1
      AND (:review_type IS NULL OR review_type = :review_type)
      AND (:status IS NULL OR status = :status)
      AND (:target_type IS NULL OR target_type = :target_type)
      AND (:created_after IS NULL OR created_at >= :created_after)
      AND (:created_before IS NULL OR created_at <= :created_before)
""")

_QUERY_REVIEW_ITEMS = text("""
    SELECT
        review_item_id,
        project_id,
        review_type,
        target_type,
        target_id,
        target_version,
        status,
        priority,
        requester_actor_type,
        requester_actor_id,
        reviewer_id,
        decision,
        reason,
        decision_payload,
        due_at,
        decided_at,
        expires_at,
        correlation_id,
        request_id,
        idempotency_key,
        created_at,
        updated_at
    FROM review_items
    WHERE 1=1
      AND (:review_type IS NULL OR review_type = :review_type)
      AND (:status IS NULL OR status = :status)
      AND (:target_type IS NULL OR target_type = :target_type)
      AND (:created_after IS NULL OR created_at >= :created_after)
      AND (:created_before IS NULL OR created_at <= :created_before)
    ORDER BY priority ASC, created_at DESC
    LIMIT :limit OFFSET :offset
""")

_GET_REVIEW_ITEM_BY_ID = text("""
    SELECT
        review_item_id,
        project_id,
        review_type,
        target_type,
        target_id,
        target_version,
        status,
        priority,
        requester_actor_type,
        requester_actor_id,
        reviewer_id,
        decision,
        reason,
        decision_payload,
        due_at,
        decided_at,
        expires_at,
        correlation_id,
        request_id,
        idempotency_key,
        created_at,
        updated_at
    FROM review_items
    WHERE review_item_id = :review_item_id
""")

_GET_REVIEW_ITEM_FOR_UPDATE_PG = text("""
    SELECT
        review_item_id,
        status
    FROM review_items
    WHERE review_item_id = :review_item_id
    FOR UPDATE
""")

_GET_REVIEW_ITEM_FOR_UPDATE_SQLITE = text("""
    SELECT
        review_item_id,
        status
    FROM review_items
    WHERE review_item_id = :review_item_id
""")

_INSERT_REVIEW_ITEM = text("""
    INSERT INTO review_items (
        review_item_id,
        project_id,
        review_type,
        target_type,
        target_id,
        status,
        priority,
        requester_actor_type,
        requester_actor_id,
        due_at,
        expires_at,
        decision_payload,
        correlation_id,
        request_id,
        idempotency_key
    ) VALUES (
        :review_item_id,
        :project_id,
        :review_type,
        :target_type,
        :target_id,
        :status,
        :priority,
        :requester_actor_type,
        :requester_actor_id,
        :due_at,
        :expires_at,
        :decision_payload,
        :correlation_id,
        :request_id,
        :idempotency_key
    )
    RETURNING review_item_id
""").bindparams(
    bindparam("decision_payload", type_=JSONB),
)

_APPROVE_REVIEW_ITEM = text("""
    UPDATE review_items
    SET status = 'approved',
        decision = 'approved',
        reviewer_id = :reviewer_id,
        reason = :reason,
        decided_at = :decided_at,
        updated_at = :updated_at
    WHERE review_item_id = :review_item_id
      AND status = 'in_review'
""")

_REJECT_REVIEW_ITEM = text("""
    UPDATE review_items
    SET status = 'rejected',
        decision = 'rejected',
        reviewer_id = :reviewer_id,
        reason = :reason,
        decided_at = :decided_at,
        updated_at = :updated_at
    WHERE review_item_id = :review_item_id
      AND status = 'in_review'
""")

_CANCEL_REVIEW_ITEM = text("""
    UPDATE review_items
    SET status = 'cancelled',
        decision = 'cancelled',
        decided_at = :decided_at,
        updated_at = :updated_at
    WHERE review_item_id = :review_item_id
      AND status IN ('pending', 'in_review')
""")

_MOVE_TO_IN_REVIEW = text("""
    UPDATE review_items
    SET status = 'in_review',
        updated_at = :updated_at
    WHERE review_item_id = :review_item_id
      AND status = 'pending'
""")

# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────


def get_review_items(
    *,
    page: int = 1,
    page_size: int = 50,
    review_type: str | None = None,
    status: str | None = None,
    target_type: str | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
) -> tuple[list[dict], int]:
    """Return a page of review items with the given filters."""
    params = {
        "review_type": review_type,
        "status": status,
        "target_type": target_type,
        "created_after": created_after,
        "created_before": created_before,
        "limit": page_size,
        "offset": (page - 1) * page_size,
    }

    with SessionLocal() as db:
        total = db.execute(_COUNT_REVIEW_ITEMS, params).scalar_one()
        rows = db.execute(_QUERY_REVIEW_ITEMS, params).mappings().all()
        items = [_row_to_dict(row) for row in rows]
        return items, total


def get_review_item_by_id(review_item_id: UUID) -> dict | None:
    """Return a single review item row by primary key, or ``None``."""
    with SessionLocal() as db:
        row = (
            db.execute(
                _GET_REVIEW_ITEM_BY_ID,
                {"review_item_id": review_item_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        return _row_to_dict(row)


def create_review_item(
    *,
    project_id: UUID | None = None,
    review_type: str,
    target_type: str,
    target_id: UUID,
    status: str = "pending",
    priority: int = 100,
    requester_actor_type: str = "system",
    requester_actor_id: UUID | None = None,
    due_at: datetime | None = None,
    expires_at: datetime | None = None,
    decision_payload: dict | None = None,
    correlation_id: UUID,
    request_id: UUID,
    idempotency_key: str,
) -> dict:
    """Insert a new review_item and return the created row as a dict.

    Parameters
    ----------
    All parameters map directly to DDL columns.  Callers must provide
    values that satisfy the CHECK constraints.
    """
    new_review_id = uuid4()
    with SessionLocal() as db:
        new_id_raw = db.execute(
            _INSERT_REVIEW_ITEM,
            {
                "review_item_id": new_review_id,
                "project_id": project_id,
                "review_type": review_type,
                "target_type": target_type,
                "target_id": target_id,
                "status": status,
                "priority": priority,
                "requester_actor_type": requester_actor_type,
                "requester_actor_id": requester_actor_id,
                "due_at": due_at,
                "expires_at": expires_at,
                "decision_payload": decision_payload or {},
                "correlation_id": correlation_id,
                "request_id": request_id,
                "idempotency_key": idempotency_key,
            },
        ).scalar_one()
        db.commit()

    return get_review_item_by_id(_as_uuid(new_id_raw))


def approve_review_item(
    review_item_id: UUID,
    reviewer_id: UUID,
    reason: str | None = None,
) -> bool:
    """Approve a review item (``in_review`` → ``approved``).

    Returns ``True`` if the update affected a row, ``False`` if the
    review item was not in ``in_review`` status.
    """
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        result = db.execute(
            _APPROVE_REVIEW_ITEM,
            {
                "review_item_id": review_item_id,
                "reviewer_id": reviewer_id,
                "reason": reason,
                "decided_at": now,
                "updated_at": now,
            },
        )
        db.commit()
        return result.rowcount > 0


def reject_review_item(
    review_item_id: UUID,
    reviewer_id: UUID,
    reason: str | None = None,
) -> bool:
    """Reject a review item (``in_review`` → ``rejected``).

    Returns ``True`` if the update affected a row, ``False`` if the
    review item was not in ``in_review`` status.
    """
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        result = db.execute(
            _REJECT_REVIEW_ITEM,
            {
                "review_item_id": review_item_id,
                "reviewer_id": reviewer_id,
                "reason": reason,
                "decided_at": now,
                "updated_at": now,
            },
        )
        db.commit()
        return result.rowcount > 0


def cancel_review_item(review_item_id: UUID) -> bool:
    """Cancel a review item (``pending`` or ``in_review`` → ``cancelled``).

    Returns ``True`` if the update affected a row, ``False`` otherwise.
    """
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        result = db.execute(
            _CANCEL_REVIEW_ITEM,
            {
                "review_item_id": review_item_id,
                "decided_at": now,
                "updated_at": now,
            },
        )
        db.commit()
        return result.rowcount > 0


def move_to_in_review(review_item_id: UUID) -> bool:
    """Transition a review item from ``pending`` to ``in_review``.

    Returns ``True`` if the update affected a row, ``False`` otherwise.
    """
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        result = db.execute(
            _MOVE_TO_IN_REVIEW,
            {
                "review_item_id": review_item_id,
                "updated_at": now,
            },
        )
        db.commit()
        return result.rowcount > 0


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────


def _row_to_dict(row) -> dict:
    """Convert a SQLAlchemy RowMapping to a plain dict safe for JSON."""
    payload = row.get("decision_payload")
    if isinstance(payload, str):
        # SQLite stores JSON fields as TEXT strings
        import json
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            payload = {}
    elif not isinstance(payload, dict):
        payload = {}

    return {
        "review_item_id": str(row["review_item_id"]),
        "project_id": str(row["project_id"])
        if row.get("project_id") is not None
        else None,
        "review_type": row["review_type"],
        "target_type": row["target_type"],
        "target_id": str(row["target_id"]),
        "target_version": row.get("target_version"),
        "status": row["status"],
        "priority": row["priority"],
        "requester_actor_type": row["requester_actor_type"],
        "requester_actor_id": str(row["requester_actor_id"])
        if row.get("requester_actor_id") is not None
        else None,
        "reviewer_id": str(row["reviewer_id"])
        if row.get("reviewer_id") is not None
        else None,
        "decision": row.get("decision"),
        "reason": row.get("reason"),
        "decision_payload": payload,
        "due_at": _isoformat(row.get("due_at")),
        "decided_at": _isoformat(row.get("decided_at")),
        "expires_at": _isoformat(row.get("expires_at")),
        "correlation_id": str(row["correlation_id"]),
        "request_id": str(row["request_id"]),
        "idempotency_key": row["idempotency_key"],
        "created_at": _isoformat(row.get("created_at")),
        "updated_at": _isoformat(row.get("updated_at")),
    }


def _isoformat(dt: datetime | str | None) -> str | None:
    """Return ISO-8601 string for *dt*, or ``None``.

    Handles both Python ``datetime`` objects (PostgreSQL) and ISO strings
    (SQLite stores timestamps as ``TEXT``).
    """
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    return dt.isoformat()


def _as_uuid(value) -> UUID:
    """Coerce a value to UUID."""
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


# ──────────────────────────────────────────────────────────────────────────────
# Batch operations
# ──────────────────────────────────────────────────────────────────────────────


def batch_claim_review_items(
    review_item_ids: list[UUID],
) -> dict:
    """Batch claim: transition multiple review items ``pending`` → ``in_review``.

    Returns a dict with summary counts and per-item results.
    """
    results = []
    succeeded = 0
    skipped = 0
    failed = 0
    for rid in review_item_ids:
        row = get_review_item_by_id(rid)
        if row is None:
            failed += 1
            results.append({
                "review_item_id": str(rid),
                "status": "failed",
                "error": f"review_item {rid} not found",
            })
            continue
        if row["status"] != "pending":
            skipped += 1
            results.append({
                "review_item_id": str(rid),
                "status": "skipped",
                "new_status": row["status"],
                "error": f"status is '{row['status']}', not 'pending'",
            })
            continue
        ok = move_to_in_review(rid)
        if ok:
            succeeded += 1
            results.append({
                "review_item_id": str(rid),
                "status": "succeeded",
                "new_status": "in_review",
            })
        else:
            failed += 1
            results.append({
                "review_item_id": str(rid),
                "status": "failed",
                "error": "concurrent modification",
            })
    return {
        "total": len(review_item_ids),
        "succeeded": succeeded,
        "skipped": skipped,
        "failed": failed,
        "results": results,
    }


def batch_approve_review_items(
    review_item_ids: list[UUID],
    reviewer_id: UUID,
    reason: str | None = None,
) -> dict:
    """Batch approve: transition multiple review items ``in_review`` → ``approved``."""
    results = []
    succeeded = 0
    skipped = 0
    failed = 0
    for rid in review_item_ids:
        row = get_review_item_by_id(rid)
        if row is None:
            failed += 1
            results.append({
                "review_item_id": str(rid),
                "status": "failed",
                "error": f"review_item {rid} not found",
            })
            continue
        if row["status"] != "in_review":
            skipped += 1
            results.append({
                "review_item_id": str(rid),
                "status": "skipped",
                "new_status": row["status"],
                "error": f"status is '{row['status']}', not 'in_review'",
            })
            continue
        ok = approve_review_item(
            review_item_id=rid,
            reviewer_id=reviewer_id,
            reason=reason,
        )
        if ok:
            succeeded += 1
            results.append({
                "review_item_id": str(rid),
                "status": "succeeded",
                "new_status": "approved",
            })
        else:
            failed += 1
            results.append({
                "review_item_id": str(rid),
                "status": "failed",
                "error": "concurrent modification",
            })
    return {
        "total": len(review_item_ids),
        "succeeded": succeeded,
        "skipped": skipped,
        "failed": failed,
        "results": results,
    }


def batch_reject_review_items(
    review_item_ids: list[UUID],
    reviewer_id: UUID,
    reason: str | None = None,
) -> dict:
    """Batch reject: transition multiple review items ``in_review`` → ``rejected``."""
    results = []
    succeeded = 0
    skipped = 0
    failed = 0
    for rid in review_item_ids:
        row = get_review_item_by_id(rid)
        if row is None:
            failed += 1
            results.append({
                "review_item_id": str(rid),
                "status": "failed",
                "error": f"review_item {rid} not found",
            })
            continue
        if row["status"] != "in_review":
            skipped += 1
            results.append({
                "review_item_id": str(rid),
                "status": "skipped",
                "new_status": row["status"],
                "error": f"status is '{row['status']}', not 'in_review'",
            })
            continue
        ok = reject_review_item(
            review_item_id=rid,
            reviewer_id=reviewer_id,
            reason=reason,
        )
        if ok:
            succeeded += 1
            results.append({
                "review_item_id": str(rid),
                "status": "succeeded",
                "new_status": "rejected",
            })
        else:
            failed += 1
            results.append({
                "review_item_id": str(rid),
                "status": "failed",
                "error": "concurrent modification",
            })
    return {
        "total": len(review_item_ids),
        "succeeded": succeeded,
        "skipped": skipped,
        "failed": failed,
        "results": results,
    }


def batch_cancel_review_items(
    review_item_ids: list[UUID],
) -> dict:
    """Batch cancel: transition multiple review items → ``cancelled``."""
    results = []
    succeeded = 0
    skipped = 0
    failed = 0
    for rid in review_item_ids:
        row = get_review_item_by_id(rid)
        if row is None:
            failed += 1
            results.append({
                "review_item_id": str(rid),
                "status": "failed",
                "error": f"review_item {rid} not found",
            })
            continue
        if row["status"] not in ("pending", "in_review"):
            skipped += 1
            results.append({
                "review_item_id": str(rid),
                "status": "skipped",
                "new_status": row["status"],
                "error": f"status is '{row['status']}', cannot cancel",
            })
            continue
        ok = cancel_review_item(rid)
        if ok:
            succeeded += 1
            results.append({
                "review_item_id": str(rid),
                "status": "succeeded",
                "new_status": "cancelled",
            })
        else:
            failed += 1
            results.append({
                "review_item_id": str(rid),
                "status": "failed",
                "error": "concurrent modification",
            })
    return {
        "total": len(review_item_ids),
        "succeeded": succeeded,
        "skipped": skipped,
        "failed": failed,
        "results": results,
    }
