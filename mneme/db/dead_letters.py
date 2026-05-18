"""P2-03 Dead Letter Queue data-access layer.

Provides pure-SQL queries against the ``dead_letters`` table used by the
admin API routes.  All queries use SQLAlchemy ``text()`` so they align
exactly with the DDL column names in ``0001_baseline_45_tables.py``.
"""

from __future__ import annotations

import logging
from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from mneme.db.base import SessionLocal

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# SQL templates
# ──────────────────────────────────────────────────────────────────────────────

_COUNT_DEAD_LETTERS = text("""
    SELECT count(*) AS total
    FROM dead_letters
    WHERE 1=1
      AND (:failure_class IS NULL OR failure_class = :failure_class)
      AND (:replay_state IS NULL OR replay_state = :replay_state)
      AND (:source_type IS NULL OR source_type = :source_type)
      AND (:created_after IS NULL OR created_at >= :created_after)
      AND (:created_before IS NULL OR created_at <= :created_before)
""")

_QUERY_DEAD_LETTERS = text("""
    SELECT
        dead_letter_id,
        source_type,
        source_id,
        related_event_id,
        aggregate_type,
        aggregate_id,
        failure_class,
        error_code,
        error_message,
        retry_exhausted,
        external_effect_state,
        replay_state,
        review_required,
        payload_json,
        first_failed_at,
        last_failed_at,
        replayed_at,
        resolved_at,
        created_at,
        updated_at
    FROM dead_letters
    WHERE 1=1
      AND (:failure_class IS NULL OR failure_class = :failure_class)
      AND (:replay_state IS NULL OR replay_state = :replay_state)
      AND (:source_type IS NULL OR source_type = :source_type)
      AND (:created_after IS NULL OR created_at >= :created_after)
      AND (:created_before IS NULL OR created_at <= :created_before)
    ORDER BY created_at DESC
    LIMIT :limit OFFSET :offset
""")

_GET_DEAD_LETTER_BY_ID = text("""
    SELECT
        dead_letter_id,
        source_type,
        source_id,
        related_event_id,
        aggregate_type,
        aggregate_id,
        failure_class,
        error_code,
        error_message,
        retry_exhausted,
        external_effect_state,
        replay_state,
        review_required,
        payload_json,
        first_failed_at,
        last_failed_at,
        replayed_at,
        resolved_at,
        created_at,
        updated_at
    FROM dead_letters
    WHERE dead_letter_id = :dead_letter_id
""")


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────


def get_dead_letters(
    *,
    page: int = 1,
    page_size: int = 50,
    failure_class: str | None = None,
    replay_state: str | None = None,
    source_type: str | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
) -> tuple[list[dict], int]:
    """Return a page of dead-letter records with the given filters.

    Parameters
    ----------
    page : int
        1-based page number.
    page_size : int
        Number of rows per page (1–200).
    failure_class : str | None
        Filter by ``failure_class``.
    replay_state : str | None
        Filter by ``replay_state``.
    source_type : str | None
        Filter by ``source_type``.
    created_after : datetime | None
        Return rows created at or after this timestamp.
    created_before : datetime | None
        Return rows created at or before this timestamp.

    Returns
    -------
    tuple[list[dict], int]
        ``(rows, total_count)`` where each row is a dict keyed by DDL column name.
    """
    params = {
        "failure_class": failure_class,
        "replay_state": replay_state,
        "source_type": source_type,
        "created_after": created_after,
        "created_before": created_before,
        "limit": page_size,
        "offset": (page - 1) * page_size,
    }

    with SessionLocal() as db:
        # Count total matching rows
        total = db.execute(_COUNT_DEAD_LETTERS, params).scalar_one()

        # Fetch the current page
        rows = db.execute(_QUERY_DEAD_LETTERS, params).mappings().all()

        items = []
        for row in rows:
            items.append(_row_to_dict(row))

        return items, total


def get_dead_letter_by_id(dead_letter_id: UUID) -> dict | None:
    """Return a single dead-letter row by primary key, or ``None``."""
    with SessionLocal() as db:
        row = (
            db.execute(
                _GET_DEAD_LETTER_BY_ID,
                {"dead_letter_id": dead_letter_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        return _row_to_dict(row)


# ──────────────────────────────────────────────────────────────────────────────
# P2-04 DLQ replay helpers
# ──────────────────────────────────────────────────────────────────────────────

_UPDATE_REPLAY_STATE = text("""
    UPDATE dead_letters
    SET replay_state = :replay_state,
        updated_at = CURRENT_TIMESTAMP
    WHERE dead_letter_id = :dead_letter_id
      AND replay_state = :expected_state
""")

_COUNT_REVIEWS_FOR_DL = text("""
    SELECT count(*) AS total
    FROM review_items
    WHERE target_type = 'dead_letter'
      AND target_id = :dead_letter_id
      AND status NOT IN ('rejected', 'cancelled', 'expired')
""")


def update_replay_state(
    dead_letter_id: UUID,
    new_state: str,
    expected_state: str,
) -> bool:
    """Atomically update ``replay_state`` on a dead_letter row.

    Uses a WHERE clause on ``expected_state`` to prevent concurrent
    modification.

    Returns ``True`` if a row was updated, ``False`` otherwise.
    """
    with SessionLocal() as db:
        result = db.execute(
            _UPDATE_REPLAY_STATE,
            {
                "dead_letter_id": dead_letter_id,
                "replay_state": new_state,
                "expected_state": expected_state,
            },
        )
        db.commit()
        return result.rowcount > 0


def count_active_reviews_for_dead_letter(dead_letter_id: UUID) -> int:
    """Return the number of active (non-final) review items for a dead_letter.

    Used to enforce the constraint: "同一 dead_letter 不能创建两个并行的
    replay review".
    """
    with SessionLocal() as db:
        total = db.execute(
            _COUNT_REVIEWS_FOR_DL,
            {"dead_letter_id": dead_letter_id},
        ).scalar_one()
        return total


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────


def _row_to_dict(row) -> dict:
    """Convert a SQLAlchemy RowMapping to a plain dict safe for JSON.

    Handles:
    - UUID → str conversion (the API schema coerces back to UUID)
    - datetime → ISO-format string (or None)
    - jsonb → dict (or empty dict)
    """
    payload = row.get("payload_json")
    if not isinstance(payload, dict):
        payload = {}

    return {
        "dead_letter_id": str(row["dead_letter_id"]),
        "source_type": row["source_type"],
        "source_id": str(row["source_id"]),
        "related_event_id": str(row["related_event_id"])
        if row.get("related_event_id") is not None
        else None,
        "aggregate_type": row.get("aggregate_type"),
        "aggregate_id": str(row["aggregate_id"])
        if row.get("aggregate_id") is not None
        else None,
        "failure_class": row["failure_class"],
        "error_code": row.get("error_code"),
        "error_message": row["error_message"],
        "retry_exhausted": bool(row["retry_exhausted"]),
        "external_effect_state": row["external_effect_state"],
        "replay_state": row["replay_state"],
        "review_required": bool(row["review_required"]),
        "payload_json": payload,
        "first_failed_at": _isoformat(row.get("first_failed_at")),
        "last_failed_at": _isoformat(row.get("last_failed_at")),
        "replayed_at": _isoformat(row.get("replayed_at")),
        "resolved_at": _isoformat(row.get("resolved_at")),
        "created_at": _isoformat(row.get("created_at")),
        "updated_at": _isoformat(row.get("updated_at")),
    }


def _isoformat(dt: datetime | None) -> str | None:
    """Return ISO-8601 string for *dt*, or ``None``."""
    if dt is None:
        return None
    return dt.isoformat()
