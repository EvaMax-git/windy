"""P2-13 Budget data-access layer -- ``usage_limits`` CRUD + ``budget_tracking``.

P2-12 skeleton kept the basic budget tracking insert/update and a placeholder
budget check that always allowed.  P2-13 fills in:

* Full ``usage_limits`` CRUD (create/read/update/delete/list).
* Actual ``check_budget_allow`` that queries ``usage_limits`` and aggregates
  committed costs from ``budget_tracking`` within the configured window.
* ``get_limit_usage`` to answer ``GET /gateway/limits/{id}/usage``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID

from mneme.db.base import SessionLocal

logger = logging.getLogger(__name__)

# -- SQL templates: budget_tracking ---------------------------------------------

_BUDGET_TRACKING_COLS = [
    "budget_tracking_id", "request_id", "correlation_id",
    "subject_type", "subject_id", "capability_id", "provider_id", "project_id",
    "reservation_state", "currency_code",
    "estimated_input_tokens", "estimated_output_tokens",
    "actual_input_tokens", "actual_output_tokens",
    "reserved_cost", "committed_cost", "released_cost",
    "denied_reason", "provider_request_fingerprint",
    "metadata_json", "created_at", "updated_at",
]

_INSERT_BUDGET = text("""
    INSERT INTO budget_tracking (
        budget_tracking_id, request_id, correlation_id,
        subject_type, subject_id, capability_id, provider_id, project_id,
        reservation_state, currency_code,
        estimated_input_tokens, estimated_output_tokens,
        reserved_cost, provider_request_fingerprint, metadata_json
    ) VALUES (
        :budget_tracking_id, :request_id, :correlation_id,
        :subject_type, :subject_id, :capability_id, :provider_id, :project_id,
        :reservation_state, :currency_code,
        :estimated_input_tokens, :estimated_output_tokens,
        :reserved_cost, :provider_request_fingerprint, :metadata_json
    )
    RETURNING budget_tracking_id
""").bindparams(
    bindparam("subject_id", ),
    bindparam("capability_id", ),
    bindparam("provider_id", ),
    bindparam("project_id", ),
    bindparam("metadata_json", type_=JSONB),
)

_UPDATE_BUDGET_STATE = text("""
    UPDATE budget_tracking
    SET reservation_state = :new_state,
        actual_input_tokens = COALESCE(:actual_input_tokens, actual_input_tokens),
        actual_output_tokens = COALESCE(:actual_output_tokens, actual_output_tokens),
        committed_cost = COALESCE(:committed_cost, committed_cost),
        released_cost = COALESCE(:released_cost, released_cost),
        denied_reason = COALESCE(:denied_reason, denied_reason),
        updated_at = CURRENT_TIMESTAMP
    WHERE budget_tracking_id = :budget_tracking_id
      AND reservation_state = :expected_state
    RETURNING budget_tracking_id
""").bindparams(
    bindparam("budget_tracking_id", ),
)

_SELECT_BUDGET_BY_ID = text("""
    SELECT budget_tracking_id, request_id, correlation_id,
           subject_type, subject_id, capability_id, provider_id, project_id,
           reservation_state, currency_code,
           estimated_input_tokens, estimated_output_tokens,
           actual_input_tokens, actual_output_tokens,
           reserved_cost, committed_cost, released_cost,
           denied_reason, provider_request_fingerprint,
           metadata_json, created_at, updated_at
    FROM budget_tracking
    WHERE budget_tracking_id = :budget_tracking_id
""").bindparams(bindparam("budget_tracking_id", ))


# -- SQL templates: usage_limits ------------------------------------------------

_USAGE_LIMIT_COLS = [
    "usage_limit_id", "subject_type", "subject_id",
    "capability_id", "provider_id", "project_id",
    "limit_scope", "window_unit",
    "max_requests", "max_input_tokens", "max_output_tokens", "max_total_tokens",
    "max_cost", "approval_threshold_cost", "block_threshold_cost",
    "enabled", "created_at", "updated_at",
]

_INSERT_LIMIT = text("""
    INSERT INTO usage_limits (
        usage_limit_id, subject_type, subject_id,
        capability_id, provider_id, project_id,
        limit_scope, window_unit,
        max_requests, max_input_tokens, max_output_tokens, max_total_tokens,
        max_cost, approval_threshold_cost, block_threshold_cost,
        enabled
    ) VALUES (
        :usage_limit_id, :subject_type, :subject_id,
        :capability_id, :provider_id, :project_id,
        :limit_scope, :window_unit,
        :max_requests, :max_input_tokens, :max_output_tokens, :max_total_tokens,
        :max_cost, :approval_threshold_cost, :block_threshold_cost,
        :enabled
    )
    RETURNING usage_limit_id
""").bindparams(
    bindparam("subject_id", ),
    bindparam("capability_id", ),
    bindparam("provider_id", ),
    bindparam("project_id", ),
)

_SELECT_LIMIT_BY_ID = text("""
    SELECT usage_limit_id, subject_type, subject_id,
           capability_id, provider_id, project_id,
           limit_scope, window_unit,
           max_requests, max_input_tokens, max_output_tokens, max_total_tokens,
           max_cost, approval_threshold_cost, block_threshold_cost,
           enabled, created_at, updated_at
    FROM usage_limits
    WHERE usage_limit_id = :usage_limit_id
""").bindparams(bindparam("usage_limit_id", ))

_UPDATE_LIMIT = text("""
    UPDATE usage_limits
    SET max_requests            = COALESCE(:max_requests, max_requests),
        max_input_tokens        = COALESCE(:max_input_tokens, max_input_tokens),
        max_output_tokens       = COALESCE(:max_output_tokens, max_output_tokens),
        max_total_tokens        = COALESCE(:max_total_tokens, max_total_tokens),
        max_cost                = COALESCE(:max_cost, max_cost),
        approval_threshold_cost = COALESCE(:approval_threshold_cost, approval_threshold_cost),
        block_threshold_cost    = COALESCE(:block_threshold_cost, block_threshold_cost),
        enabled                 = COALESCE(:enabled, enabled),
        window_unit             = COALESCE(:window_unit, window_unit),
        updated_at              = CURRENT_TIMESTAMP
    WHERE usage_limit_id = :usage_limit_id
    RETURNING usage_limit_id
""").bindparams(bindparam("usage_limit_id", ))

_DELETE_LIMIT = text("""
    DELETE FROM usage_limits
    WHERE usage_limit_id = :usage_limit_id
    RETURNING usage_limit_id
""").bindparams(bindparam("usage_limit_id", ))

# -- Query: matching limits for budget check -----------------------------------

_MATCH_LIMITS_FOR_CHECK = text("""
    SELECT usage_limit_id, subject_type, subject_id,
           capability_id, provider_id, project_id,
           limit_scope, window_unit,
           max_requests, max_input_tokens, max_output_tokens, max_total_tokens,
           max_cost, approval_threshold_cost, block_threshold_cost,
           enabled, created_at, updated_at
    FROM usage_limits
    WHERE enabled = true
      AND (
          (subject_type = :subject_type AND subject_id = :subject_id)
          OR (subject_type = :subject_type_alt AND limit_scope = 'global')
      )
      AND (:capability_id IS NULL OR capability_id IS NULL OR capability_id = :capability_id)
      AND (:provider_id IS NULL OR provider_id IS NULL OR provider_id = :provider_id)
      AND (:project_id IS NULL OR project_id IS NULL OR project_id = :project_id)
    ORDER BY
        CASE WHEN subject_type = :subject_type AND subject_id = :subject_id THEN 0 ELSE 1 END,
        created_at ASC
""").bindparams(
    bindparam("subject_id", ),
    bindparam("capability_id", ),
    bindparam("provider_id", ),
    bindparam("project_id", ),
)

# -- Query: aggregated committed usage in a time window -------------------------

_AGG_COMMITTED_USAGE = text("""
    SELECT
        COUNT(*)                                                 AS total_requests,
        COALESCE(SUM(COALESCE(actual_input_tokens, estimated_input_tokens, 0)), 0)    AS total_input_tokens,
        COALESCE(SUM(COALESCE(actual_output_tokens, estimated_output_tokens, 0)), 0)  AS total_output_tokens,
        COALESCE(SUM(COALESCE(actual_input_tokens, estimated_input_tokens, 0)
                    + COALESCE(actual_output_tokens, estimated_output_tokens, 0)), 0) AS total_total_tokens,
        COALESCE(SUM(committed_cost), 0)                                              AS total_committed_cost
    FROM budget_tracking
    WHERE reservation_state = 'committed'
      AND created_at >= :window_start
      AND subject_type = :subject_type
      AND subject_id = :subject_id
      AND (:capability_id IS NULL OR capability_id = :capability_id)
      AND (:provider_id IS NULL OR provider_id = :provider_id)
      AND (:project_id IS NULL OR project_id = :project_id)
""").bindparams(
    bindparam("subject_id", ),
    bindparam("capability_id", ),
    bindparam("provider_id", ),
    bindparam("project_id", ),
)


# -- Helpers --------------------------------------------------------------------

def _row_to_dict(row, column_names: list[str]) -> dict:
    import json as _json_mod

    _bool_cols = {"enabled"}
    d: dict = {}
    m = row._mapping if hasattr(row, "_mapping") else row
    for col in column_names:
        val = m.get(col)
        if isinstance(val, UUID):
            d[col] = str(val)
        elif isinstance(val, datetime):
            d[col] = val.isoformat()
        elif isinstance(val, str) and (col.endswith("_json") or col == "metadata_json"):
            try:
                d[col] = _json_mod.loads(val)
            except (_json_mod.JSONDecodeError, TypeError):
                d[col] = val
        elif col in _bool_cols and val is not None:
            d[col] = bool(val)
        else:
            d[col] = val
    return d


def _window_start_for(now: datetime, window_unit: str) -> datetime:
    """Return the inclusive start of the time window based on *window_unit*."""
    window_unit = window_unit.lower()
    if window_unit == "minute":
        return now - timedelta(minutes=1)
    elif window_unit == "hour":
        return now - timedelta(hours=1)
    elif window_unit == "day":
        return now - timedelta(days=1)
    elif window_unit == "month":
        return now - timedelta(days=30)
    else:
        return now - timedelta(days=1)


# -- Public API: budget_tracking ------------------------------------------------


def reserve_budget(
    *,
    request_id: UUID,
    correlation_id: UUID,
    subject_type: str,
    subject_id: UUID,
    capability_id: UUID | None = None,
    provider_id: UUID | None = None,
    project_id: UUID | None = None,
    currency_code: str = "USD",
    estimated_input_tokens: int | None = None,
    estimated_output_tokens: int | None = None,
    reserved_cost: float = 0.0,
    provider_request_fingerprint: str = "",
    metadata_json: dict[str, Any] | None = None,
) -> UUID:
    """Insert a ``budget_tracking`` row with ``reservation_state = 'reserved'``.

    Returns the ``budget_tracking_id``.
    """
    budget_tracking_id = uuid4()
    with SessionLocal() as db:
        try:
            new_id = db.execute(
                _INSERT_BUDGET,
                {
                    "budget_tracking_id": budget_tracking_id,
                    "request_id": request_id,
                    "correlation_id": correlation_id,
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    "capability_id": capability_id,
                    "provider_id": provider_id,
                    "project_id": project_id,
                    "reservation_state": "reserved",
                    "currency_code": currency_code,
                    "estimated_input_tokens": estimated_input_tokens,
                    "estimated_output_tokens": estimated_output_tokens,
                    "reserved_cost": reserved_cost,
                    "provider_request_fingerprint": provider_request_fingerprint,
                    "metadata_json": metadata_json or {},
                },
            ).scalar_one()
            db.commit()
            logger.debug("budget reserved: %s cost=%s", new_id, reserved_cost)
            return new_id
        except Exception:
            db.rollback()
            raise


def transition_budget_state(
    *,
    budget_tracking_id: UUID,
    new_state: str,
    expected_state: str,
    actual_input_tokens: int | None = None,
    actual_output_tokens: int | None = None,
    committed_cost: float | None = None,
    released_cost: float | None = None,
    denied_reason: str | None = None,
) -> bool:
    """Transition a budget_tracking row to a new reservation_state.

    Must match expected_state as optimistic concurrency guard.
    """
    with SessionLocal() as db:
        result = db.execute(
            _UPDATE_BUDGET_STATE,
            {
                "budget_tracking_id": budget_tracking_id,
                "new_state": new_state,
                "expected_state": expected_state,
                "actual_input_tokens": actual_input_tokens,
                "actual_output_tokens": actual_output_tokens,
                "committed_cost": committed_cost,
                "released_cost": released_cost,
                "denied_reason": denied_reason,
            },
        )
        db.commit()
        return result.rowcount > 0


def get_budget_tracking(budget_tracking_id: UUID) -> dict | None:
    """Fetch a budget_tracking row by primary key."""
    with SessionLocal() as db:
        row = db.execute(
            _SELECT_BUDGET_BY_ID, {"budget_tracking_id": budget_tracking_id}
        ).mappings().first()
        if row is None:
            return None
        return _row_to_dict(row, _BUDGET_TRACKING_COLS)


def check_budget_allow(
    *,
    subject_type: str,
    subject_id: UUID,
    capability_id: UUID | None = None,
    provider_id: UUID | None = None,
    project_id: UUID | None = None,
    estimated_cost: float = 0.0,
) -> tuple[bool, str | None]:
    """Check whether a call is within all matching budget limits.

    Queries ``usage_limits`` for enabled rules that match the subject
    (exact match OR global-scope project-level limits), then aggregates
    committed costs from ``budget_tracking`` within each limit's time window.

    Returns:
        (allowed, deny_reason) -- if *allowed* is False, *deny_reason* explains why.
    """
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        rows = db.execute(
            _MATCH_LIMITS_FOR_CHECK,
            {
                "subject_type": subject_type,
                "subject_id": subject_id,
                "subject_type_alt": "project",
                "capability_id": capability_id,
                "provider_id": provider_id,
                "project_id": project_id,
            },
        ).mappings().all()

        limits = [_row_to_dict(r, _USAGE_LIMIT_COLS) for r in rows]

    if not limits:
        logger.debug(
            "budget check: no matching limits for subject=%s/%s -> ALLOW",
            subject_type, subject_id,
        )
        return True, None

    for limit in limits:
        lid = limit["usage_limit_id"]
        window_unit = limit["window_unit"] or "day"
        window_start = _window_start_for(now, window_unit)

        with SessionLocal() as db:
            usage_row = db.execute(
                _AGG_COMMITTED_USAGE,
                {
                    "window_start": window_start,
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    "capability_id": limit.get("capability_id"),
                    "provider_id": limit.get("provider_id"),
                    "project_id": limit.get("project_id"),
                },
            ).mappings().first()

        current_requests = usage_row["total_requests"] if usage_row else 0
        current_input = usage_row["total_input_tokens"] if usage_row else 0
        current_output = usage_row["total_output_tokens"] if usage_row else 0
        current_total = usage_row["total_total_tokens"] if usage_row else 0
        current_cost = usage_row["total_committed_cost"] if usage_row else 0

        # Check block threshold first (most severe)
        block_threshold = limit.get("block_threshold_cost")
        if block_threshold is not None:
            new_cost = float(current_cost) + estimated_cost
            if new_cost > float(block_threshold):
                reason = (
                    f"budget blocked: limit={lid} window={window_unit} "
                    f"subject={subject_type}/{subject_id} "
                    f"cost={new_cost:.6f} > block_threshold={block_threshold}"
                )
                logger.warning("budget DENIED: %s", reason)
                return False, reason

        # Check max_requests
        max_req = limit.get("max_requests")
        if max_req is not None:
            if int(current_requests) >= int(max_req):
                reason = (
                    f"budget exceeded: limit={lid} window={window_unit} "
                    f"subject={subject_type}/{subject_id} "
                    f"requests={current_requests}/{max_req}"
                )
                logger.warning("budget DENIED: %s", reason)
                return False, reason

        # Check max_input_tokens
        max_in = limit.get("max_input_tokens")
        if max_in is not None:
            if int(current_input) >= int(max_in):
                reason = (
                    f"budget exceeded: limit={lid} window={window_unit} "
                    f"subject={subject_type}/{subject_id} "
                    f"input_tokens={current_input}/{max_in}"
                )
                logger.warning("budget DENIED: %s", reason)
                return False, reason

        # Check max_output_tokens
        max_out = limit.get("max_output_tokens")
        if max_out is not None:
            if int(current_output) >= int(max_out):
                reason = (
                    f"budget exceeded: limit={lid} window={window_unit} "
                    f"subject={subject_type}/{subject_id} "
                    f"output_tokens={current_output}/{max_out}"
                )
                logger.warning("budget DENIED: %s", reason)
                return False, reason

        # Check max_total_tokens
        max_tot = limit.get("max_total_tokens")
        if max_tot is not None:
            if int(current_total) >= int(max_tot):
                reason = (
                    f"budget exceeded: limit={lid} window={window_unit} "
                    f"subject={subject_type}/{subject_id} "
                    f"total_tokens={current_total}/{max_tot}"
                )
                logger.warning("budget DENIED: %s", reason)
                return False, reason

        # Check max_cost
        max_c = limit.get("max_cost")
        if max_c is not None:
            new_total = float(current_cost) + estimated_cost
            if new_total > float(max_c):
                reason = (
                    f"budget exceeded: limit={lid} window={window_unit} "
                    f"subject={subject_type}/{subject_id} "
                    f"cost={new_total:.6f}/{max_c}"
                )
                logger.warning("budget DENIED: %s", reason)
                return False, reason

        # Check approval_threshold_cost (logged but does NOT deny --
        # actual review trigger is via Policy Engine integration).
        approval = limit.get("approval_threshold_cost")
        if approval is not None and estimated_cost > float(approval):
            logger.info(
                "budget approval threshold reached: limit=%s cost=%.6f > threshold=%.6f",
                lid, estimated_cost, approval,
            )

    logger.debug(
        "budget check: all limits passed for subject=%s/%s cost=%s -> ALLOW",
        subject_type, subject_id, estimated_cost,
    )
    return True, None


# -- Public API: usage_limits CRUD ----------------------------------------------


def create_usage_limit(
    *,
    subject_type: str,
    subject_id: UUID,
    capability_id: UUID | None = None,
    provider_id: UUID | None = None,
    project_id: UUID | None = None,
    limit_scope: str = "global",
    window_unit: str = "day",
    max_requests: int | None = None,
    max_input_tokens: int | None = None,
    max_output_tokens: int | None = None,
    max_total_tokens: int | None = None,
    max_cost: float | None = None,
    approval_threshold_cost: float | None = None,
    block_threshold_cost: float | None = None,
    enabled: bool = True,
) -> dict:
    """Insert a new usage_limits row. Returns the full row as a dict."""
    usage_limit_id = uuid4()
    with SessionLocal() as db:
        try:
            new_id = db.execute(
                _INSERT_LIMIT,
                {
                    "usage_limit_id": usage_limit_id,
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    "capability_id": capability_id,
                    "provider_id": provider_id,
                    "project_id": project_id,
                    "limit_scope": limit_scope,
                    "window_unit": window_unit,
                    "max_requests": max_requests,
                    "max_input_tokens": max_input_tokens,
                    "max_output_tokens": max_output_tokens,
                    "max_total_tokens": max_total_tokens,
                    "max_cost": float(max_cost) if max_cost is not None else None,
                    "approval_threshold_cost": float(approval_threshold_cost) if approval_threshold_cost is not None else None,
                    "block_threshold_cost": float(block_threshold_cost) if block_threshold_cost is not None else None,
                    "enabled": enabled,
                },
            ).scalar_one()
            db.commit()
            logger.debug("usage_limit created: %s", new_id)
        except Exception:
            db.rollback()
            raise

    return get_usage_limit_by_id(new_id)


def get_usage_limit_by_id(usage_limit_id: UUID) -> dict | None:
    """Return a usage_limits row by primary key, or None."""
    with SessionLocal() as db:
        row = db.execute(
            _SELECT_LIMIT_BY_ID, {"usage_limit_id": usage_limit_id}
        ).mappings().first()
        if row is None:
            return None
        return _row_to_dict(row, _USAGE_LIMIT_COLS)


def get_usage_limits(
    *,
    page: int = 1,
    page_size: int = 50,
    subject_type: str | None = None,
    subject_id: UUID | None = None,
    capability_id: UUID | None = None,
    provider_id: UUID | None = None,
    project_id: UUID | None = None,
    limit_scope: str | None = None,
    enabled: bool | None = None,
) -> tuple[list[dict], int]:
    """Return paginated usage_limits rows with optional filters."""
    # Build count query
    count_clauses = ["1=1"]
    count_params: dict[str, Any] = {}

    if subject_type is not None:
        count_clauses.append("subject_type = :st")
        count_params["st"] = subject_type
    if subject_id is not None:
        count_clauses.append("subject_id = :sid")
        count_params["sid"] = subject_id
    if capability_id is not None:
        count_clauses.append("capability_id = :cid")
        count_params["cid"] = capability_id
    if provider_id is not None:
        count_clauses.append("provider_id = :pid")
        count_params["pid"] = provider_id
    if project_id is not None:
        count_clauses.append("project_id = :prid")
        count_params["prid"] = project_id
    if limit_scope is not None:
        count_clauses.append("limit_scope = :ls")
        count_params["ls"] = limit_scope
    if enabled is not None:
        count_clauses.append("enabled = :en")
        count_params["en"] = enabled

    count_where = " AND ".join(count_clauses)
    count_sql = f"SELECT count(*) FROM usage_limits WHERE {count_where}"

    # Build select query
    select_clauses = ["1=1"]
    select_params: dict[str, Any] = {
        "lim": page_size,
        "off": (page - 1) * page_size,
    }

    if subject_type is not None:
        select_clauses.append("subject_type = :st")
        select_params["st"] = subject_type
    if subject_id is not None:
        select_clauses.append("subject_id = :sid")
        select_params["sid"] = subject_id
    if capability_id is not None:
        select_clauses.append("capability_id = :cid")
        select_params["cid"] = capability_id
    if provider_id is not None:
        select_clauses.append("provider_id = :pid")
        select_params["pid"] = provider_id
    if project_id is not None:
        select_clauses.append("project_id = :prid")
        select_params["prid"] = project_id
    if limit_scope is not None:
        select_clauses.append("limit_scope = :ls")
        select_params["ls"] = limit_scope
    if enabled is not None:
        select_clauses.append("enabled = :en")
        select_params["en"] = enabled

    select_where = " AND ".join(select_clauses)
    select_sql = f"""
        SELECT usage_limit_id, subject_type, subject_id,
               capability_id, provider_id, project_id,
               limit_scope, window_unit,
               max_requests, max_input_tokens, max_output_tokens, max_total_tokens,
               max_cost, approval_threshold_cost, block_threshold_cost,
               enabled, created_at, updated_at
        FROM usage_limits
        WHERE {select_where}
        ORDER BY created_at DESC
        LIMIT :lim OFFSET :off
    """

    with SessionLocal() as db:
        total = db.execute(text(count_sql), count_params).scalar_one()
        rows = db.execute(text(select_sql), select_params).mappings().all()
        items = [_row_to_dict(row, _USAGE_LIMIT_COLS) for row in rows]
        return items, total


def update_usage_limit(
    usage_limit_id: UUID,
    *,
    max_requests: int | None = None,
    max_input_tokens: int | None = None,
    max_output_tokens: int | None = None,
    max_total_tokens: int | None = None,
    max_cost: float | None = None,
    approval_threshold_cost: float | None = None,
    block_threshold_cost: float | None = None,
    enabled: bool | None = None,
    window_unit: str | None = None,
) -> bool:
    """Update an existing usage_limits row. Returns True if a row was updated."""
    with SessionLocal() as db:
        result = db.execute(
            _UPDATE_LIMIT,
            {
                "usage_limit_id": usage_limit_id,
                "max_requests": max_requests,
                "max_input_tokens": max_input_tokens,
                "max_output_tokens": max_output_tokens,
                "max_total_tokens": max_total_tokens,
                "max_cost": max_cost,
                "approval_threshold_cost": approval_threshold_cost,
                "block_threshold_cost": block_threshold_cost,
                "enabled": enabled,
                "window_unit": window_unit,
            },
        )
        db.commit()
        return result.rowcount > 0


def delete_usage_limit(usage_limit_id: UUID) -> bool:
    """Delete a usage_limits row. Returns True if a row was deleted."""
    with SessionLocal() as db:
        result = db.execute(
            _DELETE_LIMIT, {"usage_limit_id": usage_limit_id}
        )
        db.commit()
        return result.rowcount > 0


def get_limit_usage(usage_limit_id: UUID) -> dict | None:
    """Return current usage summary for a specific usage limit.

    Aggregates committed costs/tokens/requests from ``budget_tracking``
    within the limit's configured ``window_unit``.

    Returns None if the limit does not exist.
    """
    limit = get_usage_limit_by_id(usage_limit_id)
    if limit is None:
        return None

    now = datetime.now(timezone.utc)
    window_unit = limit.get("window_unit", "day")
    window_start = _window_start_for(now, window_unit)

    with SessionLocal() as db:
        usage_row = db.execute(
            _AGG_COMMITTED_USAGE,
            {
                "window_start": window_start,
                "subject_type": limit["subject_type"],
                "subject_id": limit["subject_id"],
                "capability_id": limit.get("capability_id"),
                "provider_id": limit.get("provider_id"),
                "project_id": limit.get("project_id"),
            },
        ).mappings().first()

    return {
        "usage_limit_id": str(usage_limit_id),
        "window_unit": window_unit,
        "window_start": window_start.isoformat(),
        "window_end": now.isoformat(),
        "total_requests": usage_row["total_requests"] if usage_row else 0,
        "total_input_tokens": usage_row["total_input_tokens"] if usage_row else 0,
        "total_output_tokens": usage_row["total_output_tokens"] if usage_row else 0,
        "total_total_tokens": usage_row["total_total_tokens"] if usage_row else 0,
        "total_committed_cost": float(usage_row["total_committed_cost"]) if usage_row else 0.0,
        "limits": limit,
    }
