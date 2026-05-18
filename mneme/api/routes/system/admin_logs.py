"""Admin log filtering endpoints.

Endpoints
---------
* ``GET /api/v4/admin/logs`` — paginated API call logs with filters
  (level, source, since/until time range, call_type)

Design
------
Uses the ``api_call_logs`` table as the source of truth.  Each row tracks
a single Gateway API call with its state machine, token usage, cost, and
latency.  The ``call_state`` column serves as the "level" (e.g. succeeded,
failed, timeout).
"""

from __future__ import annotations

import math
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from mneme.api.context import RequestContext, get_request_context
from mneme.api.schemas import envelope
from mneme.db.base import get_db
from mneme.schemas.admin_logs import (
    AdminLogEntry,
    AdminLogFilterParams,
    AdminLogListResponse,
)
from mneme.schemas.common import PageInfo, PaginationParams

router = APIRouter(prefix="/admin/logs", tags=["admin", "logs"])


def _row_to_entry(row) -> AdminLogEntry:
    """Map a raw DB row dict to AdminLogEntry."""

    def _maybe_uuid(val):
        if val is None:
            return None
        if isinstance(val, UUID):
            return val
        try:
            return UUID(str(val))
        except (ValueError, TypeError):
            return None

    return AdminLogEntry(
        api_call_log_id=_maybe_uuid(row["api_call_log_id"]),
        request_id=_maybe_uuid(row.get("request_id")),
        correlation_id=_maybe_uuid(row.get("correlation_id")),
        actor_type=row.get("actor_type", "system"),
        provider_id=_maybe_uuid(row.get("provider_id")),
        call_type=row.get("call_type", "chat"),
        call_state=row.get("call_state", "planned"),
        input_tokens=row.get("input_tokens"),
        output_tokens=row.get("output_tokens"),
        total_tokens=row.get("total_tokens"),
        latency_ms=row.get("latency_ms"),
        error_code=row.get("error_code"),
        error_message=row.get("error_message"),
        retry_count=row.get("retry_count", 0),
        started_at=row.get("started_at"),
        finished_at=row.get("finished_at"),
        created_at=row.get("created_at"),
    )


def _page_info(total: int, page: int, page_size: int) -> PageInfo:
    """Build a PageInfo model for paginated responses."""
    total_pages = max(1, math.ceil(total / max(page_size, 1)))
    return PageInfo(
        page=page,
        page_size=page_size,
        total_items=total,
        total_pages=total_pages,
        has_next=page < total_pages,
        has_previous=page > 1,
    )


@router.get("", response_model=dict)
def list_logs(
    pagination: PaginationParams = Depends(),
    filters: AdminLogFilterParams = Depends(),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """List API call logs with optional filtering by level, source, and time.

    Query parameters
    ----------------
    * ``page`` / ``page_size`` — pagination (default 1 / 50, max 200).
    * ``level`` — filter by call_state: succeeded, failed, timeout, cancelled,
      denied, dead_letter, in_flight, budget_reserved, credential_checked.
    * ``source`` — filter by actor_type (e.g. "user", "agent", "system") or
      provider_id UUID.
    * ``since`` — ISO-8601 timestamp; return logs created at or after this time.
    * ``until`` — ISO-8601 timestamp; return logs created at or before this time.
    * ``call_type`` — filter by call_type: chat, embedding, completion, etc.
    """
    offset = (max(pagination.page, 1) - 1) * pagination.page_size

    # Build dynamic WHERE clause
    conditions = []
    params: dict = {}

    if filters.level:
        conditions.append("call_state = :level")
        params["level"] = filters.level

    if filters.source:
        # Try matching as UUID (provider_id) first, then as actor_type
        is_uuid = False
        try:
            UUID(filters.source)
            is_uuid = True
        except ValueError:
            pass

        if is_uuid:
            conditions.append("provider_id = :source_uuid")
            params["source_uuid"] = filters.source
        else:
            conditions.append("actor_type = :source_actor")
            params["source_actor"] = filters.source

    if filters.since:
        conditions.append("created_at >= :since")
        params["since"] = filters.since

    if filters.until:
        conditions.append("created_at <= :until")
        params["until"] = filters.until

    if filters.call_type:
        conditions.append("call_type = :call_type")
        params["call_type"] = filters.call_type

    where_clause = " AND ".join(conditions) if conditions else "TRUE"

    # Count
    count_sql = text(f"SELECT COUNT(*) FROM api_call_logs WHERE {where_clause}")
    total = db.execute(count_sql, params).scalar_one()

    # Query
    query_sql = text(f"""
        SELECT
            api_call_log_id, request_id, correlation_id,
            actor_type, provider_id,
            call_type, call_state,
            input_tokens, output_tokens, total_tokens,
            latency_ms, error_code, error_message, retry_count,
            started_at, finished_at, created_at
        FROM api_call_logs
        WHERE {where_clause}
        ORDER BY created_at DESC
        LIMIT :limit OFFSET :offset
    """)

    params["limit"] = pagination.page_size
    params["offset"] = offset

    rows = db.execute(query_sql, params).mappings().all()
    items = [_row_to_entry(r) for r in rows]

    pi = _page_info(total, pagination.page, pagination.page_size)
    data = AdminLogListResponse(items=items, page_info=pi)

    return envelope(
        data.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )
