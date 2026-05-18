"""P2-12 Data-access layer for ``api_call_logs`` table.

Provides atomic insert/update operations for the 10-state call pipeline:

    planned → budget_reserved → credential_checked → in_flight → succeeded / failed
    Terminal: cancelled / denied / timeout / dead_letter

Every transition is written via a dedicated helper that enforces valid state
transitions at the application layer.
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

# ── Valid state transitions ────────────────────────────────────────────────────

_VALID_TRANSITIONS: dict[str, set[str]] = {
    "planned":           {"budget_reserved", "cancelled", "denied"},
    "budget_reserved":   {"credential_checked", "cancelled", "denied"},
    "credential_checked": {"in_flight", "cancelled", "denied"},
    "in_flight":         {"succeeded", "failed", "timeout", "cancelled"},
    "failed":            {"in_flight", "dead_letter", "cancelled"},
    "succeeded":         set(),
    "cancelled":         set(),
    "denied":            set(),
    "timeout":           {"in_flight", "dead_letter"},
    "dead_letter":       set(),
}


# ── SQL templates ──────────────────────────────────────────────────────────────

_API_CALL_LOG_COLS = [
    "api_call_log_id", "request_id", "correlation_id", "idempotency_key",
    "project_id", "actor_type", "actor_id", "auth_context_type", "auth_context_id",
    "capability_id", "capability_binding_id", "provider_id", "provider_model_id",
    "credential_id", "vault_access_log_id", "budget_tracking_id", "review_item_id",
    "event_id", "call_type", "call_state", "external_request_id",
    "provider_request_fingerprint", "request_summary", "response_summary",
    "input_tokens", "output_tokens", "total_tokens",
    "estimated_cost", "actual_cost", "currency_code",
    "latency_ms", "retry_count", "error_code", "error_message",
    "retention_until", "started_at", "finished_at", "created_at", "updated_at",
]

_INSERT_CALL_LOG = text("""
    INSERT INTO api_call_logs (
        api_call_log_id, request_id, correlation_id, idempotency_key,
        project_id, actor_type, actor_id, auth_context_type, auth_context_id,
        capability_id, capability_binding_id, provider_id, provider_model_id,
        credential_id, call_type, call_state,
        provider_request_fingerprint, request_summary, currency_code,
        retention_until
    ) VALUES (
        :api_call_log_id, :request_id, :correlation_id, :idempotency_key,
        :project_id, :actor_type, :actor_id, :auth_context_type, :auth_context_id,
        :capability_id, :capability_binding_id, :provider_id, :provider_model_id,
        :credential_id, :call_type, :call_state,
        :provider_request_fingerprint, :request_summary, :currency_code,
        :retention_until
    )
    RETURNING api_call_log_id
""").bindparams(
    bindparam("request_summary", type_=JSONB),
)

_UPDATE_CALL_STATE = text("""
    UPDATE api_call_logs
    SET call_state       = :new_state,
        updated_at       = CURRENT_TIMESTAMP,
        started_at       = CASE
            WHEN :new_state = 'in_flight' THEN COALESCE(started_at, CURRENT_TIMESTAMP)
            ELSE started_at END,
        finished_at      = CASE
            WHEN :new_state IN ('succeeded', 'failed', 'cancelled', 'denied', 'timeout', 'dead_letter')
            THEN CURRENT_TIMESTAMP
            ELSE finished_at END
    WHERE api_call_log_id = :api_call_log_id
      AND call_state = :expected_state
    RETURNING api_call_log_id
""").bindparams(
    bindparam("api_call_log_id", ),
)

_UPDATE_CALL_RESULT = text("""
    UPDATE api_call_logs
    SET call_state        = :new_state,
        external_request_id = :external_request_id,
        input_tokens       = COALESCE(:input_tokens, input_tokens),
        output_tokens      = COALESCE(:output_tokens, output_tokens),
        total_tokens       = COALESCE(:total_tokens, total_tokens),
        estimated_cost     = COALESCE(:estimated_cost, estimated_cost),
        actual_cost        = COALESCE(:actual_cost, actual_cost),
        latency_ms         = COALESCE(:latency_ms, latency_ms),
        error_code         = :error_code,
        error_message      = :error_message,
        response_summary   = :response_summary,
        finished_at        = CURRENT_TIMESTAMP,
        updated_at         = CURRENT_TIMESTAMP
    WHERE api_call_log_id = :api_call_log_id
    RETURNING api_call_log_id
""").bindparams(
    bindparam("api_call_log_id", ),
    bindparam("response_summary", type_=JSONB),
)

_UPDATE_CALL_BUDGET_ID = text("""
    UPDATE api_call_logs
    SET budget_tracking_id = :budget_tracking_id,
        updated_at = CURRENT_TIMESTAMP
    WHERE api_call_log_id = :api_call_log_id
""").bindparams(
    bindparam("api_call_log_id", ),
    bindparam("budget_tracking_id", ),
)

_UPDATE_CALL_CREDENTIAL = text("""
    UPDATE api_call_logs
    SET credential_id = :credential_id,
        vault_access_log_id = :vault_access_log_id,
        updated_at = CURRENT_TIMESTAMP
    WHERE api_call_log_id = :api_call_log_id
""").bindparams(
    bindparam("api_call_log_id", ),
    bindparam("credential_id", ),
    bindparam("vault_access_log_id", ),
)

_UPDATE_RETRY_COUNT = text("""
    UPDATE api_call_logs
    SET retry_count = retry_count + 1,
        updated_at = CURRENT_TIMESTAMP
    WHERE api_call_log_id = :api_call_log_id
""").bindparams(
    bindparam("api_call_log_id", ),
)

_SELECT_CALL_LOG_BY_ID = text("""
    SELECT api_call_log_id, request_id, correlation_id, idempotency_key,
           project_id, actor_type, actor_id, auth_context_type, auth_context_id,
           capability_id, capability_binding_id, provider_id, provider_model_id,
           credential_id, vault_access_log_id, budget_tracking_id, review_item_id,
           event_id, call_type, call_state, external_request_id,
           provider_request_fingerprint, request_summary, response_summary,
           input_tokens, output_tokens, total_tokens,
           estimated_cost, actual_cost, currency_code,
           latency_ms, retry_count, error_code, error_message,
           retention_until, started_at, finished_at, created_at, updated_at
    FROM api_call_logs
    WHERE api_call_log_id = :api_call_log_id
""").bindparams(bindparam("api_call_log_id", ))


# ── Helpers ────────────────────────────────────────────────────────────────────

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
        elif isinstance(val, str) and (col.endswith("_json") or col in ("request_summary", "response_summary")):
            try:
                d[col] = _json_mod.loads(val)
            except (_json_mod.JSONDecodeError, TypeError):
                d[col] = val
        else:
            d[col] = val
    return d


def _validate_transition(current_state: str, new_state: str) -> None:
    """Raise ValueError if the transition is invalid."""
    allowed = _VALID_TRANSITIONS.get(current_state, set())
    if new_state not in allowed:
        raise ValueError(
            f"Invalid state transition: {current_state} -> {new_state}. "
            f"Allowed: {allowed or 'none (terminal state)'}"
        )


# ── Public API ─────────────────────────────────────────────────────────────────


def insert_api_call_log(
    *,
    request_id: UUID,
    correlation_id: UUID,
    idempotency_key: str,
    project_id: UUID | None = None,
    actor_type: str = "system",
    actor_id: UUID | None = None,
    auth_context_type: str | None = None,
    auth_context_id: UUID | None = None,
    capability_id: UUID,
    capability_binding_id: UUID | None = None,
    provider_id: UUID,
    provider_model_id: UUID | None = None,
    credential_id: UUID | None = None,
    call_type: str = "chat",
    call_state: str = "planned",
    provider_request_fingerprint: str = "",
    request_summary: dict[str, Any] | None = None,
    currency_code: str = "USD",
    retention_days: int = 180,
) -> UUID:
    """Insert a new ``api_call_logs`` row and return its primary key."""
    retention_until = datetime.now(timezone.utc) + timedelta(days=retention_days)
    api_call_log_id = uuid4()

    with SessionLocal() as db:
        try:
            new_id = db.execute(
                _INSERT_CALL_LOG,
                {
                    "api_call_log_id": api_call_log_id,
                    "request_id": request_id,
                    "correlation_id": correlation_id,
                    "idempotency_key": idempotency_key,
                    "project_id": project_id,
                    "actor_type": actor_type,
                    "actor_id": actor_id,
                    "auth_context_type": auth_context_type,
                    "auth_context_id": auth_context_id,
                    "capability_id": capability_id,
                    "capability_binding_id": capability_binding_id,
                    "provider_id": provider_id,
                    "provider_model_id": provider_model_id,
                    "credential_id": credential_id,
                    "call_type": call_type,
                    "call_state": call_state,
                    "provider_request_fingerprint": provider_request_fingerprint,
                    "request_summary": request_summary or {},
                    "currency_code": currency_code,
                    "retention_until": retention_until,
                },
            ).scalar_one()
            db.commit()
            logger.debug("api_call_log created: %s state=%s", new_id, call_state)
            return new_id
        except Exception:
            db.rollback()
            raise


def transition_call_state(
    *,
    api_call_log_id: UUID,
    new_state: str,
    expected_state: str,
) -> bool:
    """Transition an ``api_call_logs`` row to a new call_state.

    The UPDATE is conditional on the current state matching *expected_state*
    (optimistic concurrency guard).

    Returns True if a row was updated, False otherwise.
    """
    _validate_transition(expected_state, new_state)

    with SessionLocal() as db:
        result = db.execute(
            _UPDATE_CALL_STATE,
            {
                "api_call_log_id": api_call_log_id,
                "new_state": new_state,
                "expected_state": expected_state,
            },
        )
        db.commit()
        updated = result.rowcount > 0
        if updated:
            logger.debug(
                "api_call_log %s transition: %s -> %s",
                api_call_log_id, expected_state, new_state,
            )
        else:
            logger.warning(
                "api_call_log %s transition failed: expected %s -> %s",
                api_call_log_id, expected_state, new_state,
            )
        return updated


def update_call_result(
    *,
    api_call_log_id: UUID,
    new_state: str,
    external_request_id: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    total_tokens: int | None = None,
    estimated_cost: float | None = None,
    actual_cost: float | None = None,
    latency_ms: int | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    response_summary: dict[str, Any] | None = None,
) -> bool:
    """Update an ``api_call_logs`` row with the result of a completed call."""
    with SessionLocal() as db:
        result = db.execute(
            _UPDATE_CALL_RESULT,
            {
                "api_call_log_id": api_call_log_id,
                "new_state": new_state,
                "external_request_id": external_request_id,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "estimated_cost": estimated_cost,
                "actual_cost": actual_cost,
                "latency_ms": latency_ms,
                "error_code": error_code,
                "error_message": error_message,
                "response_summary": response_summary or {},
            },
        )
        db.commit()
        return result.rowcount > 0


def link_budget_tracking(
    *,
    api_call_log_id: UUID,
    budget_tracking_id: UUID,
) -> bool:
    """Associate a ``budget_tracking`` row with an ``api_call_logs`` row."""
    with SessionLocal() as db:
        result = db.execute(
            _UPDATE_CALL_BUDGET_ID,
            {
                "api_call_log_id": api_call_log_id,
                "budget_tracking_id": budget_tracking_id,
            },
        )
        db.commit()
        return result.rowcount > 0


def link_credential_access(
    *,
    api_call_log_id: UUID,
    credential_id: UUID,
    vault_access_log_id: UUID,
) -> bool:
    """Record the credential and vault access log id used for this call."""
    with SessionLocal() as db:
        result = db.execute(
            _UPDATE_CALL_CREDENTIAL,
            {
                "api_call_log_id": api_call_log_id,
                "credential_id": credential_id,
                "vault_access_log_id": vault_access_log_id,
            },
        )
        db.commit()
        return result.rowcount > 0


def increment_retry_count(api_call_log_id: UUID) -> bool:
    """Increment the retry_count on an api_call_logs row."""
    with SessionLocal() as db:
        result = db.execute(
            _UPDATE_RETRY_COUNT,
            {"api_call_log_id": api_call_log_id},
        )
        db.commit()
        return result.rowcount > 0


def get_api_call_log(api_call_log_id: UUID) -> dict | None:
    """Fetch an api_call_logs row by primary key."""
    with SessionLocal() as db:
        row = db.execute(
            _SELECT_CALL_LOG_BY_ID, {"api_call_log_id": api_call_log_id}
        ).mappings().first()
        if row is None:
            return None
        return _row_to_dict(row, _API_CALL_LOG_COLS)
