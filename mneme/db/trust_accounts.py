"""Data-access layer for ``trust_accounts`` table.

信任账户 — per-subject trust ledger tracking call counts, success rate,
and user feedback. The ``trust_score`` is computed as a weighted composite:

    trust_score = 0.5 * success_rate + 0.3 * feedback_ratio + 0.2 * activity_bonus

where:
  - success_rate = successful_calls / max(total_calls, 1)
  - feedback_ratio = positive_feedback / max(positive_feedback + negative_feedback, 1)
  - activity_bonus = min(total_calls / 100, 1.0)  (saturates at 100 calls)
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

_TRUST_ACCOUNT_COLS = [
    "trust_account_id", "subject_type", "subject_id", "capability_id",
    "total_calls", "successful_calls", "failed_calls",
    "success_rate", "positive_feedback", "negative_feedback",
    "neutral_feedback", "trust_score", "last_evaluated_at",
    "metadata_json", "created_at", "updated_at",
]

# ── Trust score computation ───────────────────────────────────────────────────


def _compute_trust_score(
    successful_calls: int,
    total_calls: int,
    positive_feedback: int,
    negative_feedback: int,
) -> float:
    """Compute a composite trust_score in [0, 1]."""
    denom_calls = max(total_calls, 1)
    denom_feedback = max(positive_feedback + negative_feedback, 1)

    success_rate = successful_calls / denom_calls
    feedback_ratio = positive_feedback / denom_feedback
    activity_bonus = min(total_calls / 100.0, 1.0)

    score = 0.5 * success_rate + 0.3 * feedback_ratio + 0.2 * activity_bonus
    return round(max(0.0, min(1.0, score)), 4)


# ── SQL templates ─────────────────────────────────────────────────────────────

_INSERT_ACCOUNT = text("""
    INSERT INTO trust_accounts (
        trust_account_id, subject_type, subject_id, capability_id,
        metadata_json
    ) VALUES (
        :trust_account_id, :subject_type, :subject_id, :capability_id,
        :metadata_json
    )
    RETURNING trust_account_id
""").bindparams(
    bindparam("trust_account_id", type_=PG_UUID(as_uuid=True)),
    bindparam("subject_id", type_=PG_UUID(as_uuid=True)),
    bindparam("capability_id", type_=PG_UUID(as_uuid=True)),
    bindparam("metadata_json", type_=JSONB),
)

_SELECT_BY_ID = text("""
    SELECT trust_account_id, subject_type, subject_id, capability_id,
           total_calls, successful_calls, failed_calls,
           success_rate, positive_feedback, negative_feedback,
           neutral_feedback, trust_score, last_evaluated_at,
           metadata_json, created_at, updated_at
    FROM trust_accounts
    WHERE trust_account_id = :trust_account_id
""").bindparams(bindparam("trust_account_id", type_=PG_UUID(as_uuid=True)))

_SELECT_BY_SUBJECT = text("""
    SELECT trust_account_id, subject_type, subject_id, capability_id,
           total_calls, successful_calls, failed_calls,
           success_rate, positive_feedback, negative_feedback,
           neutral_feedback, trust_score, last_evaluated_at,
           metadata_json, created_at, updated_at
    FROM trust_accounts
    WHERE subject_type = :subject_type
      AND subject_id = :subject_id
      AND (:capability_id IS NULL OR capability_id = :capability_id)
    ORDER BY created_at ASC
""").bindparams(
    bindparam("subject_id", type_=PG_UUID(as_uuid=True)),
    bindparam("capability_id", type_=PG_UUID(as_uuid=True)),
)

_RECORD_CALL = text("""
    UPDATE trust_accounts
    SET total_calls      = total_calls + 1,
        successful_calls = successful_calls + CASE WHEN :success THEN 1 ELSE 0 END,
        failed_calls     = failed_calls + CASE WHEN :success THEN 0 ELSE 1 END,
        success_rate     = CASE
            WHEN (total_calls + 1) = 0 THEN 0.0000
            ELSE ROUND(
                (successful_calls + CASE WHEN :success THEN 1 ELSE 0 END)::numeric
                / (total_calls + 1)::numeric, 4)
        END,
        trust_score      = :trust_score,
        last_evaluated_at = :evaluated_at,
        updated_at       = :evaluated_at
    WHERE trust_account_id = :trust_account_id
    RETURNING trust_account_id
""").bindparams(bindparam("trust_account_id", type_=PG_UUID(as_uuid=True)))

_RECORD_FEEDBACK = text("""
    UPDATE trust_accounts
    SET positive_feedback = positive_feedback + CASE WHEN :feedback_type = 'positive' THEN 1 ELSE 0 END,
        negative_feedback = negative_feedback + CASE WHEN :feedback_type = 'negative' THEN 1 ELSE 0 END,
        neutral_feedback  = neutral_feedback + CASE WHEN :feedback_type = 'neutral' THEN 1 ELSE 0 END,
        trust_score       = :trust_score,
        last_evaluated_at = :evaluated_at,
        updated_at        = :evaluated_at
    WHERE trust_account_id = :trust_account_id
    RETURNING trust_account_id
""").bindparams(bindparam("trust_account_id", type_=PG_UUID(as_uuid=True)))

_COUNT_TRUST_ACCOUNTS = """
    SELECT count(*) AS total
    FROM trust_accounts
    WHERE 1=1
      AND (:subject_type IS NULL OR subject_type = :subject_type)
      AND (:subject_id IS NULL OR subject_id = :subject_id)
      AND (:min_trust_score IS NULL OR trust_score >= :min_trust_score)
      AND (:max_trust_score IS NULL OR trust_score <= :max_trust_score)
"""

_SELECT_TRUST_ACCOUNTS = """
    SELECT trust_account_id, subject_type, subject_id, capability_id,
           total_calls, successful_calls, failed_calls,
           success_rate, positive_feedback, negative_feedback,
           neutral_feedback, trust_score, last_evaluated_at,
           metadata_json, created_at, updated_at
    FROM trust_accounts
    WHERE 1=1
      AND (:subject_type IS NULL OR subject_type = :subject_type)
      AND (:subject_id IS NULL OR subject_id = :subject_id)
      AND (:min_trust_score IS NULL OR trust_score >= :min_trust_score)
      AND (:max_trust_score IS NULL OR trust_score <= :max_trust_score)
"""


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
        elif col == "metadata_json":
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


def get_or_create_trust_account(
    *,
    subject_type: str,
    subject_id: UUID,
    capability_id: UUID | None = None,
    metadata_json: dict[str, Any] | None = None,
) -> dict:
    """Return existing trust_account or create a new one.

    A trust_account is unique per (subject_type, subject_id, capability_id).
    """
    # Try to find existing first
    with SessionLocal() as db:
        rows = db.execute(
            _SELECT_BY_SUBJECT,
            {
                "subject_type": subject_type,
                "subject_id": subject_id,
                "capability_id": capability_id,
            },
        ).mappings().all()

        if rows:
            return _row_to_dict(rows[0], _TRUST_ACCOUNT_COLS)

        # Create new account
        trust_account_id = uuid4()
        try:
            db.execute(
                _INSERT_ACCOUNT,
                {
                    "trust_account_id": trust_account_id,
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    "capability_id": capability_id,
                    "metadata_json": metadata_json or {},
                },
            )
            db.commit()
            logger.debug(
                "trust_account created: %s for %s/%s",
                trust_account_id, subject_type, subject_id,
            )
        except Exception:
            db.rollback()
            raise

    return get_trust_account_by_id(trust_account_id)


def get_trust_account_by_id(trust_account_id: UUID) -> dict | None:
    """Fetch a trust_accounts row by primary key."""
    with SessionLocal() as db:
        row = db.execute(
            _SELECT_BY_ID, {"trust_account_id": trust_account_id}
        ).mappings().first()
        if row is None:
            return None
        return _row_to_dict(row, _TRUST_ACCOUNT_COLS)


def get_trust_account_by_subject(
    *,
    subject_type: str,
    subject_id: UUID,
    capability_id: UUID | None = None,
) -> dict | None:
    """Fetch the trust_account for a given subject, or None."""
    with SessionLocal() as db:
        rows = db.execute(
            _SELECT_BY_SUBJECT,
            {
                "subject_type": subject_type,
                "subject_id": subject_id,
                "capability_id": capability_id,
            },
        ).mappings().all()
        if not rows:
            return None
        return _row_to_dict(rows[0], _TRUST_ACCOUNT_COLS)


def record_call(
    *,
    trust_account_id: UUID,
    success: bool = True,
) -> dict | None:
    """Record a call outcome and recompute trust_score.

    Increments total_calls and either successful_calls or failed_calls.
    Returns the updated trust_account or None if not found.
    """
    account = get_trust_account_by_id(trust_account_id)
    if account is None:
        return None

    evaluated_at = datetime.now(timezone.utc)
    new_successful = account["successful_calls"] + (1 if success else 0)
    new_total = account["total_calls"] + 1
    new_failed = account["failed_calls"] + (0 if success else 1)

    new_score = _compute_trust_score(
        successful_calls=new_successful,
        total_calls=new_total,
        positive_feedback=account["positive_feedback"],
        negative_feedback=account["negative_feedback"],
    )

    with SessionLocal() as db:
        result = db.execute(
            _RECORD_CALL,
            {
                "trust_account_id": trust_account_id,
                "success": success,
                "trust_score": new_score,
                "evaluated_at": evaluated_at,
            },
        )
        db.commit()
        if result.rowcount == 0:
            return None

    logger.debug(
        "trust_account %s call recorded: success=%s score=%.4f",
        trust_account_id, success, new_score,
    )
    return get_trust_account_by_id(trust_account_id)


def record_feedback(
    *,
    trust_account_id: UUID,
    feedback_type: str,
) -> dict | None:
    """Record user feedback (positive/negative/neutral) and recompute trust_score.

    Returns the updated trust_account or None if not found.
    """
    account = get_trust_account_by_id(trust_account_id)
    if account is None:
        return None

    if feedback_type not in ("positive", "negative", "neutral"):
        raise ValueError(f"feedback_type must be one of 'positive', 'negative', 'neutral'; got '{feedback_type}'")

    evaluated_at = datetime.now(timezone.utc)

    new_positive = account["positive_feedback"] + (1 if feedback_type == "positive" else 0)
    new_negative = account["negative_feedback"] + (1 if feedback_type == "negative" else 0)

    new_score = _compute_trust_score(
        successful_calls=account["successful_calls"],
        total_calls=account["total_calls"],
        positive_feedback=new_positive,
        negative_feedback=new_negative,
    )

    with SessionLocal() as db:
        result = db.execute(
            _RECORD_FEEDBACK,
            {
                "trust_account_id": trust_account_id,
                "feedback_type": feedback_type,
                "trust_score": new_score,
                "evaluated_at": evaluated_at,
            },
        )
        db.commit()
        if result.rowcount == 0:
            return None

    logger.debug(
        "trust_account %s feedback recorded: %s score=%.4f",
        trust_account_id, feedback_type, new_score,
    )
    return get_trust_account_by_id(trust_account_id)


def get_trust_accounts(
    *,
    page: int = 1,
    page_size: int = 50,
    subject_type: str | None = None,
    subject_id: UUID | None = None,
    min_trust_score: float | None = None,
    max_trust_score: float | None = None,
) -> tuple[list[dict], int]:
    """Return paginated trust_accounts with optional filters."""
    params: dict[str, Any] = {
        "subject_type": subject_type,
        "subject_id": subject_id,
        "min_trust_score": min_trust_score,
        "max_trust_score": max_trust_score,
        "limit": page_size,
        "offset": (page - 1) * page_size,
    }

    with SessionLocal() as db:
        total = db.execute(text(_COUNT_TRUST_ACCOUNTS), params).scalar_one()
        rows = db.execute(
            text(
                _SELECT_TRUST_ACCOUNTS
                + " ORDER BY trust_score DESC, total_calls DESC LIMIT :limit OFFSET :offset"
            ),
            params,
        ).mappings().all()
        items = [_row_to_dict(row, _TRUST_ACCOUNT_COLS) for row in rows]
        return items, total
